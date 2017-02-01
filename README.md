# Zendesk Help Center Translation Helper

[![Jenkins Job Screenshot](https://github.com/mykola-mokhnach/zendesk-help-center-localization/blob/master/jenkins.png?raw=true)](#Jenkins Job Screenshot)

## Purpose

This Python script has been created to automate Zendesk Help Center
 translation process. It uses
 [Zendesk Help Center REST API](https://developer.zendesk.com/rest_api/docs/help_center/introduction)
 and
 [Crowdin REST API](https://support.crowdin.com/api/api-integration-setup/)
 to make the communication between these two systems possible.


## Problem

This is the way we usually update the information in Zendesk Help Center:

1. Draft articles are created for new features being developed during
 the current sprint. These articles might be completely new ones or
 updated copies of existing articles.

2. After the draft articles preparation in the original language is done, we can start the
 translation.

3. After the translation process is done and new features are publicly released
 we publish the updated articles and their translated copies.

This process requires a lot of manual effort if one just does everything using the "raw"
 Help Center interface. For example, articles versioning is not supported in the
 current implementation of the Help Center API. So we had to keep all updated articles in
 a separate spreadsheet and then manually copy/paste them to Zendesk as soon as
 the corresponding product feature, which is described there, is publicly released.
 This got even worse with localization, because it was necessary to copy/paste
 _languages_count_ articles instead of one. The translation process was also done in the same
 spreadsheet, which was causing big headaches every time there were any updates to the
 original content after the translation had been already done.


## Solution

This script aims to solve the issue described above:

1. No more spreadsheets. All the Help Center content is kept where it is supposed to be
 and until it is supposed to exist.

2. HTML formatting in articles is preserved.

3. No more problems with "out of sync" articles.

4. No more "human factor" kind of errors. Most of the verification is
 done automatically.

5. The script is optimized for usage with Web-based CI systems, like [Jenkins](https://jenkins.io).
 This means anyone without command line usage experience can use it.

6. All articles and their translations are always in sync.


## Prerequisites

### Environment variables

The script receives all the necessary information from these environment variables,
 that are required to be set:

**ZENDESK_EMAIL**: Zendesk user email address. This should be a user with Admin
 privileges. It is recommended (but not mandatory) to have a separate user
 for this purpose.

**ZENDESK_API_TOKEN**: Zendesk OAuth token. This token should be created in the
 administrator console.

**ZENDESK_API_URL**: Zendesk REST API URL. The script supports version 2 of the API.

**ZENDESK_SHOULD_CLEAN_DRAFTS**: Whether to clean up draft articles after they are
 published. Can be either 'true' or 'false'.

**CROWDIN_PROJECT_NAME**: Your project name in Crowdin for API usage.

**CROWDIN_ROOT_FOLDER**: The path to the root directory in the corresponding
 Crowdin project. Create this folder in project settings if it does not exist
 yet. Do NOT manually put any files/folder under this folder, because they will
 be deleted after the next sync happens.

**CROWDIN_API_URL**: Crowdin REST API url.

**CROWDIN_API_KEY**: Your secret Crowdin API access key for the corresponding project.

**DstLanguages**: The list of destination language abbreviations supported
 by Zendesk.

**FLOW_MODE**: One of ('Create Drafts In Zendesk', 'Export Zendesk Drafts To Crowdin',
 'Import Crowdin Translations To Zendesk Drafts', 'Publish Zendesk Drafts'). This
 variable defines what is going to be done by the script.

### OS

The script has been tested on Mac OS, but it is supposed to work on any operating
 system where Python 2.7 is supported.

### Dependencies

The script depends on the [requests](http://docs.python-requests.org/en/master/) Python module,
 which can be installed by executing

```bash
pip install requests
```

### Jenkins Integration (Optional)

The following Jenkins plugins might/will be useful for a successful script integration:

 - [Python Plugin](https://wiki.jenkins-ci.org/display/JENKINS/Python+Plugin)
 - [EnvInject Plugin](https://wiki.jenkins-ci.org/display/JENKINS/EnvInject+Plugin)
 - [Parameterized Build Plugin](https://wiki.jenkins-ci.org/display/JENKINS/Parameterized+Build)
 - [Mask Passwords Plugin](https://wiki.jenkins-ci.org/display/JENKINS/Mask+Passwords+Plugin)


## Getting Started

### Step #1: Let's Create Some Drafts

There are two main types of drafts: pending articles, which are already published and need to be updated, and new
 articles, which describe completely new features. In order to create drafts for the articles of the first type one
 just has to put the magic **draft** label to such published articles. The next step would be to execute our script with
 **FLOW_MODE** set to _Create Drafts In Zendesk_, for example:

```bash
FLOW_MODE="Create Drafts In Zendesk" python zendesk_localization.py
```

This will automatically clone all the published articles marked with the magic label to drafts. Each article will also
 contain special prefix with the id of original article in the title. Please, DO NOT touch this prefix. Now one may
 start editing these newly created drafts, but only the content in original language.
The new draft articles can simply be created directly in Zendesk. It is necessary to not forget to mark them
 with the **draft** label.

### Step #2: Time For Export

After the preparation of the original content has been completed one may start the export process:

```bash
FLOW_MODE="Export Zendesk Drafts To Crowdin" python zendesk_localization.py
```

This command will automatically filter all the draft articles having the magic **draft** label and will export them
 to Crowdin. The script automatically detects if some category/section/article has been renamed or moved and sync
 it with Crowdin. Deletions are not synchronized by purpose. After the export process is finished one may initiate the
 translation into multiple languages. Crowdin will automatically detect the changed content and will keep unchanged
 content translated.

### Step #3: Let's Get The Translated Content Back

One may initiate the third step after Crowdin translation is completed:

```bash
FLOW_MODE="Import Crowdin Translations To Zendesk Drafts" DstLanguages='de,fr,it' python zendesk_localization.py
```

This will try to match all the draft articles marked with the **draft** label to Crowdin content and import translations
 for _DstLanguages_ language abbreviations. This is the best time to proofread everything and perform the necessary
 fixes in Crowdin if necessary. One may repeat the import process as many times as needed.

### Step #4: We're Almost There

We now have the content translated and verified in drafts so it's now time to present it to our customers:

```bash
FLOW_MODE="Publish Zendesk Drafts" ZENDESK_SHOULD_CLEAN_DRAFTS=true python zendesk_localization.py
```

This command will remove the **draft** label from the corresponding articles in drafts and will publish them together with
 their translations. This works a bit differently for cloned articles though. Such articles are going to be deleted
 and their content will be synchronized to the original articles. One can set **ZENDESK_SHOULD_CLEAN_DRAFTS** to
 'false' if such drafts are still needed.


## Known Issues

- One cannot change attachments (neither inline nor external) in cloned draft articles. This rule does not apply to new
 articles
- Articles content is shown as raw HTML in Crowdin. It's not so bad, since the TMS has built-in verification instruments
 for translated text verification, but might be a bit confusing for some translators
- File names in Crowdin contain only characters from \[A-Za-z_] set. This means one might have problems if the original
articles language is a language with non-latin alphabet.
