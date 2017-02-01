#!usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2016-present Mykola Mokhnach at Wire Swiss GmbH
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
# in compliance with the License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License
# is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing permissions and limitations under
# the License.


import codecs
from collections import OrderedDict
import copy
import logging
import itertools
import json
import httplib
from pprint import pformat
from cStringIO import StringIO
import re
import requests
import shutil
import tempfile
import os
import urllib
import urllib2
from zipfile import ZipFile

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
channel = logging.StreamHandler()
channel.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
channel.setFormatter(formatter)
logger.addHandler(channel)

ZENDESK_EMAIL = os.getenv('ZENDESK_EMAIL')
ZENDESK_API_TOKEN = os.getenv('ZENDESK_API_TOKEN')
ZENDESK_API_URL = os.getenv('ZENDESK_API_URL')
ZENDESK_SHOULD_CLEAN_DRAFTS = os.getenv('ZENDESK_SHOULD_CLEAN_DRAFTS', 'false').lower() == 'true'

CROWDIN_PROJECT_NAME = os.getenv('CROWDIN_PROJECT_NAME')
CROWDIN_ROOT_FOLDER = os.getenv('CROWDIN_ROOT_FOLDER')
CROWDIN_API_URL = os.getenv('CROWDIN_API_URL', 'https://api.crowdin.com/api/project')
CROWDIN_API_KEY = os.getenv('CROWDIN_API_KEY')

# Can contain only language abbreviations supported by Zendesk
DST_LANGUAGE_ABBRS = map(lambda x: x.strip(), os.getenv('DstLanguages', 'de').split(','))

# Set language abbreviations matching to this dictionary if they are different in Zendesk and Crowdin
ZENDESK_TO_CROWDIN_LANGUGES_MAPPING = {'en-us': 'en'}

CLONED_DRAFT_TITLE_PATTERN = re.compile(r'^\s*\[(\d+)\]\s+')
DRAFT_MARKER_LABEL = u'draft'

RES_EXTENSION = 'json'
FILENAME_PATTERN = re.compile(r'^(\d+)_.*\.json$', re.IGNORECASE)

# CURRENT_FLOW_MODE = 'Export Zendesk Drafts To Crowdin'
# CURRENT_FLOW_MODE = 'Import Crowdin Translations To Zendesk Drafts'
# CURRENT_FLOW_MODE = 'Publish Zendesk Drafts'
CURRENT_FLOW_MODE = os.getenv('FLOW_MODE', 'Create Drafts In Zendesk')


class APIError(Exception):
    def __init__(self, message, error_code):
        super(APIError, self).__init__(message)
        self._error_code = error_code

    @property
    def error_code(self):
        return self._error_code


class ZendeskAPI(object):
    # https://developer.zendesk.com/rest_api/docs/help_center/translations#update-translation

    def __init__(self, api_root, login, token):
        self._api_root = api_root
        self._login = login
        self._token = token

    def _get(self, endpoint):
        response = requests.request('GET',
                                    url='{}/{}'.format(self._api_root, endpoint),
                                    headers={'Content-Type': 'application/json',
                                             'Accept': 'application/json'},
                                    auth=(self._login + '/token', self._token))
        if response.status_code == httplib.OK:
            return response.json()
        raise APIError(response.text, response.status_code)

    def _post(self, endpoint, post_data=None):
        response = requests.request('POST',
                                    url='{}/{}'.format(self._api_root, endpoint),
                                    headers={'Content-Type': 'application/json',
                                             'Accept': 'application/json'},
                                    auth=(self._login + '/token', self._token),
                                    json=post_data)
        if response.status_code in (httplib.OK, httplib.CREATED):
            return response.json()
        raise APIError(response.text, response.status_code)

    def _put(self, endpoint, put_data=None):
        response = requests.request('PUT',
                                    url='{}/{}'.format(self._api_root, endpoint),
                                    headers={'Content-Type': 'application/json',
                                             'Accept': 'application/json'},
                                    auth=(self._login + '/token', self._token),
                                    json=put_data)
        if response.status_code in (httplib.OK, httplib.CREATED):
            return response.json()
        raise APIError(response.text, response.status_code)

    def _delete(self, endpoint):
        response = requests.request('DELETE',
                                    url='{}/{}'.format(self._api_root, endpoint),
                                    auth=(self._login + '/token', self._token))
        if response.status_code != httplib.NO_CONTENT:
            raise APIError(response.text, response.status_code)

    def _upload_file(self, endpoint, path, form_data=None):
        multipart_data = {}
        if form_data is not None:
            for key, value in form_data.iteritems():
                multipart_data[key] = (None, value)
        with open(path, 'rb') as src_file:
            multipart_data['file'] = src_file
            response = requests.request('POST',
                                        url='{}/{}'.format(self._api_root, endpoint),
                                        headers={'Accept': 'application/json'},
                                        auth=(self._login + '/token', self._token),
                                        files=multipart_data)
        if response.status_code in (httplib.NO_CONTENT, httplib.CREATED):
            return response.json()
        raise APIError(response.text, response.status_code)

    def list_categories(self):
        pages_count = self._get('categories.json')['page_count']
        return reduce(lambda a, b: a + self._get('categories.json?page={}'.format(b)).get('categories', []),
                      xrange(1, pages_count + 1), [])

    def list_sections(self, category_id):
        pages_count = self._get('categories/{}/sections.json'.format(category_id))['page_count']
        return reduce(lambda a, b: a + self._get('categories/{}/sections.json?page={}'.format(category_id,
                                                                                              b)).get('sections', []),
                      xrange(1, pages_count + 1), [])

    def list_articles(self, section_id):
        pages_count = self._get('sections/{}/articles.json'.format(section_id))['page_count']
        return reduce(lambda a, b: a + self._get('sections/{}/articles.json?page={}'.format(section_id,
                                                                                            b)).get('articles', []),
                      xrange(1, pages_count + 1), [])

    def find_articles(self, params_dict):
        """https://developer.zendesk.com/rest_api/docs/help_center/search

        Search result does NOT include draft articles
        """
        params_str = urllib.urlencode(params_dict)
        return self._get('articles/search.json?{}'.format(params_str)).get('results', [])

    def list_article_translations(self, article_id):
        """https://developer.zendesk.com/rest_api/docs/help_center/translations#show-translation
        """
        return self._get('articles/{}/translations.json'.format(article_id)).get('translations', [])

    def get_article_translation(self, article_id, locale_abbr):
        return self._get('articles/{}/translations/{}.json'.format(article_id, locale_abbr))['translation']

    def create_article_translation(self, article_id, locale_abbr, data):
        request_data = copy.copy(data)
        request_data.update({'locale': locale_abbr})
        return self._post('articles/{}/translations.json'.format(article_id),
                          {'translation': request_data})['translation']

    def update_article_translation(self, article_id, locale_abbr, data):
        return self._put('articles/{}/translations/{}.json'.format(article_id, locale_abbr),
                         {'translation': data})['translation']

    def create_article(self, section_id, article_properties):
        """https://developer.zendesk.com/rest_api/docs/help_center/articles#create-article
        """
        return self._post('sections/{}/articles.json'.format(section_id), {'article': article_properties})['article']

    def update_article(self, article_id, article_properties):
        """https://developer.zendesk.com/rest_api/docs/help_center/articles#update-article
        """
        return self._put('articles/{}.json'.format(article_id), {'article': article_properties})['article']

    def list_article_attachments(self, article_id):
        try:
            return self._get('articles/{}/attachments.json'.format(article_id)).get('article_attachments', [])
        except APIError as e:
            if e.error_code == httplib.NOT_FOUND:
                return []
            raise e

    def create_article_attachment(self, article_id, src_file_path, attachment_properties):
        return self._upload_file('articles/{}/attachments.json'.format(article_id),
                                 src_file_path,
                                 attachment_properties)['article_attachment']

    def delete_article(self, article_id):
        self._delete('articles/{}.json'.format(article_id))

    @classmethod
    def download_attachment(cls, src_url, file_name):
        result_path = os.path.join(tempfile.gettempdir(), file_name)
        response = urllib2.urlopen(src_url)
        with open(result_path, 'wb') as f:
            f.write(response.read())
        return result_path


class CrowdinAPI(object):
    # https://support.crowdin.com/api/api-integration-setup/

    ITEM_TYPE_FILE = 'file'
    ITEM_TYPE_FOLDER = 'directory'

    def __init__(self, api_root, project_name, token, root_folder):
        self._api_root = api_root
        self._project_name = project_name
        self._token = token
        self._root_folder = root_folder

    @property
    def project_name(self):
        return self._project_name

    def _post(self, endpoint, **post_data):
        response = requests.post('{}/{}/{}'.format(self._api_root, self._project_name, endpoint), **post_data)
        if response.status_code == httplib.OK:
            return response.json()
        raise APIError(response.text, response.status_code)

    def _get(self, endpoint):
        response = requests.get('{}/{}/{}'.format(self._api_root, self._project_name, endpoint))
        if response.status_code == httplib.OK:
            return response.json()
        raise APIError(response.text, response.status_code)

    def download_translations(self, locale):
        response = requests.get('{}/{}/download/{}.zip'.format(self._api_root, self._project_name, locale), {
            'key': self._token,
        })
        if response.status_code == httplib.OK:
            dst_folder = tempfile.mkdtemp()
            with ZipFile(StringIO(response.content), 'r') as z:
                z.extractall(dst_folder)
            return dst_folder
        raise APIError(response.text, response.status_code)

    def export_translations(self):
        return self._get('export?key={}&json=true'.format(self._token))

    def get_project_info(self):
        return self._post('info?key={}&json=true'.format(self._token))

    def _lookup_item(self, item_id, info_root, item_type, path=''):
        """
        :param item_id:
        :param info_root: structure root, the value of ['files'] key
        :param item_type: either 'file' or 'directory'
        :param path:
        :return:
        """
        for item_info in info_root:
            if item_info['name'].startswith('{}_'.format(item_id)) and item_info['node_type'] == item_type:
                return '{}/{}'.format(path, item_info['name'])
            if 'files' in item_info:
                result = self._lookup_item(item_id,
                                           item_info['files'],
                                           item_type,
                                           '{}/{}'.format(path, item_info['name']))
                if result is not None:
                    return result

    def _sync_folder(self, folder_id, expected_folder_path):
        folder_path = self._lookup_item(folder_id,
                                        self.get_project_info()['files'],
                                        self.ITEM_TYPE_FOLDER)
        if folder_path is None:
            # This is a new folder
            self._post(
                'add-directory?key={}&json=true&name={}'.format(self._token, urllib.quote(expected_folder_path))
            )
        else:
            # The folder already exists in Crowdin
            if folder_path != expected_folder_path:
                if os.path.dirname(folder_path) != os.path.dirname(expected_folder_path):
                    # The whole folder was moved
                    raise RuntimeError(u'Changing folder structure is not supported by Crowdin API. '
                                       u'Please move "{}" to "{}" manually to continue export'.
                                       format(folder_path, expected_folder_path))
                # The folder was renamed
                self._post('change-directory?key={}&json=true&name={}&new_name={}'.format(
                    self._token,
                    urllib.quote(folder_path),
                    urllib.quote(os.path.basename(expected_folder_path)))
                )

    def _sync_file(self, file_id, data_dict, expected_file_path):
        tmpname = tempfile.mkstemp('.json')[1]
        project_info = self.get_project_info()
        with codecs.open(tmpname, 'w', 'utf-8') as f:
            f.write(json.dumps(data_dict, indent=2, sort_keys=True, encoding='utf-8'))
        try:
            actual_file_path = self._lookup_item(file_id,
                                                 project_info['files'],
                                                 self.ITEM_TYPE_FILE)
            if actual_file_path is None:
                # The file does not exist in Crowdin
                with codecs.open(tmpname, 'r', 'utf-8') as fd:
                    self._post(
                        'add-file?key={}&json=true'.format(self._token),
                        files={'files[{}]'.format(expected_file_path): fd}
                    )
            else:
                # The file already exists in Crowdin
                if os.path.dirname(actual_file_path) != os.path.dirname(expected_file_path):
                    # File path is changed
                    self._post(
                        'delete-file?key={}&json=true&file={}'.format(self._token, urllib.quote(actual_file_path))
                    )
                    with codecs.open(tmpname, 'r', 'utf-8') as fd:
                        self._post('add-file?key={}&json=true'.format(self._token),
                                   files={'files[{}]'.format(expected_file_path): fd})
                    self._post('pre-translate?key={}&json=true'.format(self._token),
                               data={'files[]': expected_file_path,
                                     'languages[]': map(lambda x: x['code'], project_info['languages'])})
                elif os.path.basename(actual_file_path) != os.path.basename(expected_file_path):
                    # Only file name is changed
                    with codecs.open(tmpname, 'r', 'utf-8') as fd:
                        self._post('update-file?key={}&json=true'.format(self._token),
                                   files={'files[{}]'.format(actual_file_path): fd},
                                   data={'titles[{}]'.format(actual_file_path): os.path.basename(expected_file_path)})
                else:
                    # Path is not changed, just update the file
                    with codecs.open(tmpname, 'r', 'utf-8') as fd:
                        self._post('update-file?key={}&json=true'.format(self._token),
                                   files={'files[{}]'.format(actual_file_path): fd})
        finally:
            os.unlink(tmpname)

    @staticmethod
    def _normalize_basename(basename):
        return re.sub(r'\W', '_', basename)

    def upload_category(self, category):
        expected_category_folder_path = '/{}/{}_{}'.format(self._root_folder,
                                                           category['id'],
                                                           self._normalize_basename(category['name']))
        self._sync_folder(category['id'], expected_category_folder_path)

    def upload_section(self, parent_category, section):
        expected_section_folder_path = '/{}/{}_{}/{}_{}'.format(self._root_folder,
                                                                parent_category['id'],
                                                                self._normalize_basename(parent_category['name']),
                                                                section['id'],
                                                                self._normalize_basename(section['name']))
        self._sync_folder(section['id'], expected_section_folder_path)

    def upload_article(self, parent_category, parent_section, article, article_id=None):
        dst_id = article['id'] if article_id is None else article_id
        expected_article_path = '/{}/{}_{}/{}_{}/{}_{}.{}'.format(self._root_folder,
                                                                  parent_category['id'],
                                                                  self._normalize_basename(parent_category['name']),
                                                                  parent_section['id'],
                                                                  self._normalize_basename(parent_section['name']),
                                                                  dst_id,
                                                                  self._normalize_basename(article['title']),
                                                                  RES_EXTENSION)
        return self._sync_file(dst_id,
                               {'title': article['title'],
                                'body': article['body']},
                               expected_article_path)


def _find_draft_article(original_article, articles_in_section):
    candidate_articles = filter(lambda x: _is_draft(x) and x['id'] != original_article['id'], articles_in_section)
    for candidate_article in candidate_articles:
        match = CLONED_DRAFT_TITLE_PATTERN.search(candidate_article['title'])
        if match is not None and long(match.group(1)) == long(original_article['id']):
            return candidate_article
    return None


def _generate_draft_title(article):
    match = CLONED_DRAFT_TITLE_PATTERN.search(article['title'])
    if match is None:
        return u'[{}] {}'.format(article['id'], article['title'])
    return article['title']


def _clone_article_to_draft(zen_api, src_article):
    original_translations = zen_api.list_article_translations(src_article['id'])
    other_locale_abbrs = map(lambda x: x['locale'], original_translations)
    if src_article['source_locale'] in other_locale_abbrs:
        other_locale_abbrs.remove(src_article['source_locale'])
    cloned_article_data = [{
        'locale': src_article['source_locale'],
        'title': _generate_draft_title(src_article),
        'body': src_article['body'],
        'draft': True,
    }]
    if not set(DST_LANGUAGE_ABBRS).intersection(set(other_locale_abbrs)):
        raise RuntimeError(u'The article "{}" does not contain mandatory "{}" language(s).\n'
                           u'Please update the draft at {} and restart the process.'.
                           format(src_article['title'],
                                  list(set(DST_LANGUAGE_ABBRS) - set(other_locale_abbrs)),
                                  src_article['html_url']))
    for locale_abbr in other_locale_abbrs:
        localized_article = next(x for x in original_translations if x['locale'] == locale_abbr)
        cloned_article_data.append({
            'locale': locale_abbr,
            'title': localized_article['title'],
            'body': localized_article['body'],
            'draft': True,
        })
    cloned_article = zen_api.create_article(src_article['section_id'], {'translations': cloned_article_data})
    cloned_labels = {'label_names': src_article['label_names']}
    zen_api.update_article(cloned_article['id'], cloned_labels)
    cloned_article.update(cloned_labels)
    _remove_article_labels(zen_api, src_article, DRAFT_MARKER_LABEL)
    return cloned_article


def _sync_top_level_tree_with_crowdin(crowd_api, categories, sections):
    for category in categories:
        crowd_api.upload_category(category)
        sections_in_category = filter(lambda x: x['category_id'] == category['id'], sections)
        for section in sections_in_category:
            crowd_api.upload_section(category, section)


def _sync_article_with_crowdin(crowd_api, categories, sections, article):
    parent_section = next(x for x in sections if x['id'] == article['section_id'])
    parent_category = next(x for x in categories if x['id'] == parent_section['category_id'])
    article_to_export = copy.copy(article)
    original_article_id = _extract_article_id_from_title(article_to_export)
    if original_article_id is not None:
        article_to_export['title'] = _restore_original_title(article_to_export)
    crowd_api.upload_article(parent_category, parent_section, article_to_export, original_article_id)


def _extract_article_id_from_title(article):
    match = CLONED_DRAFT_TITLE_PATTERN.search(article['title'])
    return long(match.group(1)) if match else None


def _extract_article_id_from_filename(fname):
    match = FILENAME_PATTERN.search(fname)
    return long(match.group(1)) if match else None


def _import_translation_to_zendesk(zen_api, lang_abbr, path, dst_article):
    with codecs.open(path, 'r', 'utf-8') as fd:
        translated_article = json.load(fd, encoding='utf-8')
        translated_article['draft'] = True
        try:
            zen_api.update_article_translation(dst_article['id'], lang_abbr, translated_article)
        except APIError as e:
            if 'RecordNotFound' in e.message:
                zen_api.create_article_translation(dst_article['id'], lang_abbr, translated_article)
            else:
                raise e
        dst_article.update(translated_article)
        return dst_article


def create_zendesk_drafts(zen_api):
    candidate_articles = zen_api.find_articles({'label_names': DRAFT_MARKER_LABEL})
    if candidate_articles:
        logger.info(u'Found {} article(s) ready for making drafts'.format(len(candidate_articles)))
    else:
        logger.info(u'No articles found to make drafts from')
        return []
    articles_by_section_id = {}
    processed_articles = []
    for candidate_article in candidate_articles:
        if candidate_article['section_id'] not in articles_by_section_id:
            articles_by_section_id[candidate_article['section_id']] = \
                zen_api.list_articles(candidate_article['section_id'])
        cloned_article = _find_draft_article(candidate_article,
                                             articles_by_section_id[candidate_article['section_id']])
        if cloned_article is not None:
            logger.warning(u'The original "{}" article has already been already cloned as {}\n'
                           u'Consider removing "{}" label from the original article {}\n'.
                           format(candidate_article['title'],
                                  cloned_article['html_url'],
                                  DRAFT_MARKER_LABEL,
                                  candidate_article['html_url']))
            continue
        logger.info(u'Creating draft for the article "{}" at {}...'.format(candidate_article['title'],
                                                                           candidate_article['html_url']))
        draft_article = _clone_article_to_draft(zen_api, candidate_article)
        logger.info(u'Successfully created draft article "{}" at {}'.format(draft_article['title'],
                                                                            draft_article['html_url']))
        processed_articles.append(draft_article)
    return processed_articles


def _is_draft(article):
    return article['draft'] is True and DRAFT_MARKER_LABEL in article['label_names']


def export_zendesk_drafts_to_crowdin(zen_api, crowd_api):
    all_categories = zen_api.list_categories()
    all_sections = reduce(lambda a, b: a + zen_api.list_sections(b['id']), all_categories, [])
    logger.info(u'Synchronizing folder structure with Crowdin...')
    _sync_top_level_tree_with_crowdin(crowd_api, all_categories, all_sections)
    logger.info(u'Folder structure synchronization is completed\n')
    all_articles = reduce(lambda a, b: a + zen_api.list_articles(b['id']), all_sections, [])
    draft_articles = filter(_is_draft, all_articles)
    if draft_articles:
        logger.info(u'Found {} draft article(s) to export\n'.format(len(draft_articles), pformat(draft_articles)))
    else:
        logger.info(u'No draft articles found. Nothing to export')
        return []
    processed_articles = []
    for draft_article in draft_articles:
        logger.info(u'Exporting article "{}" from {}...'.format(draft_article['title'],
                                                                draft_article['html_url']))
        _sync_article_with_crowdin(crowd_api, all_categories, all_sections, draft_article)
        processed_articles.append(draft_article)
        logger.info(u'The article "{}" has been successfully exported to Crowdin at https://crowdin.com/project/{}\n'.
                    format(draft_article['title'], crowd_api.project_name))
    return processed_articles


def import_drafts_from_crowdin_to_zendesk(crowd_api, zen_api):
    all_categories = zen_api.list_categories()
    all_sections = reduce(lambda a, b: a + zen_api.list_sections(b['id']), all_categories, [])
    all_articles = reduce(lambda a, b: a + zen_api.list_articles(b['id']), all_sections, [])
    draft_articles = filter(_is_draft, all_articles)
    if not draft_articles:
        logger.info(u'No draft articles have been found. Nothing to import\n')
        return []
    processed_article_by_id = OrderedDict()
    crowd_api.export_translations()
    for dst_language_abbr in DST_LANGUAGE_ABBRS:
        language_abbr_in_crowdin = ZENDESK_TO_CROWDIN_LANGUGES_MAPPING.get(dst_language_abbr, dst_language_abbr)
        root = crowd_api.download_translations(language_abbr_in_crowdin)
        try:
            for current_root, dirs, files in os.walk(root):
                for fname in files:
                    full_path = os.path.join(current_root, fname)
                    article_id = _extract_article_id_from_filename(fname)
                    if article_id is None:
                        # logger.info('Cannot parse id from {}. Skipping...\n'.format(full_path))
                        continue
                    dst_article = next(itertools.ifilter(
                        lambda x: _extract_article_id_from_title(x) == article_id or long(x['id']) == article_id,
                        draft_articles), None)
                    if dst_article is None:
                        if dst_article is None:
                            logger.warning(
                                u'Cannot find Zendesk draft with id "{}" for {}. Skipping...\n'.format(article_id,
                                                                                                       full_path)
                            )
                            continue
                    logger.info(u'Importing {} (locale {})...'.format(full_path.replace(root, ''), dst_language_abbr))
                    _import_translation_to_zendesk(zen_api, dst_language_abbr, full_path, dst_article)
                    logger.info(u'Successfully updated draft article "{}" for locale "{}" at {}\n'.
                                format(dst_article['title'], dst_language_abbr, dst_article['html_url']))
                    processed_article_by_id[dst_article['id']] = dst_article
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return processed_article_by_id.values()


def _find_original_article(draft_article, all_articles):
    match = CLONED_DRAFT_TITLE_PATTERN.search(draft_article['title'])
    if match is not None:
        draft_id = long(match.group(1))
        return next(itertools.ifilter(lambda x: long(x['id']) == draft_id, all_articles), None)
    return None


def _restore_original_title(article):
    result = article['title']
    match = CLONED_DRAFT_TITLE_PATTERN.search(result)
    if match is not None:
        result = CLONED_DRAFT_TITLE_PATTERN.split(result)[-1]
    return result


def _remove_article_labels(zen_api, article, labels_to_remove):
    if isinstance(labels_to_remove, basestring):
        labels_to_remove = [labels_to_remove]
    result_labels = filter(lambda x: x not in labels_to_remove, article['label_names'])
    if len(result_labels) != len(article['label_names']):
        return zen_api.update_article(article['id'], {'label_names': result_labels})
    return article


def _is_draft_different_from_original(draft_translations, original_translations):
    draft_locale_abbrs = map(lambda x: x['locale'], draft_translations)
    original_locale_abbrs = map(lambda x: x['locale'], original_translations)
    common_locale_abbrs = set(draft_locale_abbrs).intersection(set(original_locale_abbrs))
    for locale_abbr in common_locale_abbrs:
        draft_entry = next(x for x in draft_translations if x['locale'] == locale_abbr)
        original_entry = next(x for x in original_translations if x['locale'] == locale_abbr)
        if _restore_original_title(draft_entry) != original_entry['title'] \
                or draft_entry['body'] != original_entry['body']:
            return True
    return False


def _publish_draft_article(zen_api, draft_src_article, original_article, should_clean_draft):
    draft_translations = zen_api.list_article_translations(draft_src_article['id'])
    other_locale_abbrs = map(lambda x: x['locale'], draft_translations)
    if draft_src_article['source_locale'] in other_locale_abbrs:
        other_locale_abbrs.remove(draft_src_article['source_locale'])
    common_locale_abbrs = list(set(DST_LANGUAGE_ABBRS).intersection(set(other_locale_abbrs)))
    if not common_locale_abbrs:
        logger.error(u'The draft article "{}" does not contain mandatory "{}" language(s).\n'
                     u'Please update the draft at {}\n'.
                     format(draft_src_article['title'], common_locale_abbrs, draft_src_article['html_url']))
        return None
    if original_article is not None:
        logger.info(u'Found original article "{}" at {}'.format(original_article['title'],
                                                                original_article['html_url']))
        original_translations = zen_api.list_article_translations(original_article['id'])
        if not _is_draft_different_from_original(draft_translations, original_translations):
            logger.info(u'The draft article "{}" at {} seems to be equal to the published one. Skipping...'
                        .format(draft_src_article['title'], draft_src_article['html_url']))
            return None
        logger.info(u'Replacing...')
        source_locale_properties = {'title': _restore_original_title(draft_src_article),
                                    'body': draft_src_article['body'],
                                    'draft': False}
        zen_api.update_article_translation(original_article['id'], original_article['source_locale'],
                                           source_locale_properties)
        original_article.update(source_locale_properties)
        for locale_abbr in common_locale_abbrs:
            src_translation = next(x for x in draft_translations if x['locale'] == locale_abbr)
            zen_api.update_article_translation(original_article['id'], locale_abbr,
                                               {'title': src_translation['title'],
                                                'body': src_translation['body'],
                                                'draft': False})
        original_article = _remove_article_labels(zen_api, original_article, DRAFT_MARKER_LABEL)
        if should_clean_draft is True:
            logger.info(u'Removed obsolete draft article "{}" at {}'.format(draft_src_article['title'],
                                                                            draft_src_article['html_url']))
            zen_api.delete_article(draft_src_article['id'])
        else:
            logger.info(u'Draft articles removal is disabled. Keeping "{}" at {}'.format(draft_src_article['title'],
                                                                                         draft_src_article['html_url']))
        return original_article
    source_locale_properties = {'title': _restore_original_title(draft_src_article),
                                'draft': False}
    zen_api.update_article_translation(draft_src_article['id'], draft_src_article['source_locale'],
                                       source_locale_properties)
    draft_src_article.update(source_locale_properties)
    for locale_abbr in common_locale_abbrs:
        zen_api.update_article_translation(draft_src_article['id'], locale_abbr, {'draft': False})
    return _remove_article_labels(zen_api, draft_src_article, DRAFT_MARKER_LABEL)


def publish_zendesk_drafts(zen_api, should_clean_drafts):
    all_categories = zen_api.list_categories()
    all_sections = reduce(lambda a, b: a + zen_api.list_sections(b['id']), all_categories, [])
    all_articles = reduce(lambda a, b: a + zen_api.list_articles(b['id']), all_sections, [])
    draft_articles = filter(_is_draft, all_articles)
    if draft_articles:
        logger.info(u'Found {} draft article(s) to publish\n'.format(len(draft_articles)))
    else:
        logger.info(u'No draft articles found. Nothing to publish\n')
        return []
    published_articles = []
    for draft_article in draft_articles:
        logger.info(u'Publishing draft article "{}" at {}...'.format(draft_article['title'],
                                                                     draft_article['html_url']))
        original_article = _find_original_article(draft_article, all_articles)
        published_article = _publish_draft_article(zen_api, draft_article, original_article, should_clean_drafts)
        if published_article is not None:
            # noinspection PyUnresolvedReferences
            logger.info(u'Successfully published the draft as "{}" at {}\n'.format(published_article['title'],
                                                                                   published_article['html_url']))
            published_articles.append(published_article)
    return published_articles


if __name__ == '__main__':
    zendesk_api = ZendeskAPI(ZENDESK_API_URL, ZENDESK_EMAIL, ZENDESK_API_TOKEN)
    crowdin_api = CrowdinAPI(CROWDIN_API_URL, CROWDIN_PROJECT_NAME, CROWDIN_API_KEY, CROWDIN_ROOT_FOLDER)

    processed_items = []
    if CURRENT_FLOW_MODE == 'Create Drafts In Zendesk':
        processed_items = create_zendesk_drafts(zendesk_api)
    elif CURRENT_FLOW_MODE == 'Export Zendesk Drafts To Crowdin':
        processed_items = export_zendesk_drafts_to_crowdin(zendesk_api, crowdin_api)
    elif CURRENT_FLOW_MODE == 'Import Crowdin Translations To Zendesk Drafts':
        processed_items = import_drafts_from_crowdin_to_zendesk(crowdin_api, zendesk_api)
    elif CURRENT_FLOW_MODE == 'Publish Zendesk Drafts':
        processed_items = publish_zendesk_drafts(zendesk_api, ZENDESK_SHOULD_CLEAN_DRAFTS)
    else:
        raise AttributeError(u'Unknown flow mode "{}"'.format(CURRENT_FLOW_MODE))

    if processed_items:
        if len(processed_items) == 1:
            logger.info(u'1 item has been successfully processed')
        else:
            logger.info(u'{} items have been successfully processed'.format(len(processed_items)))
