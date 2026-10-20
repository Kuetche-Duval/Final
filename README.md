# Newton's Nerds 📚💡

**Study Hard, Pray Harder.**  
*A student-built study hub for learning, sharing knowledge, and growing together.*

> “Love one another as I have loved you.” ❤️❤️❤️

Newton's Nerds is an educational web application created to make studying more organized, collaborative, and accessible for students. The project brings learning resources and study tools into one place, with a focus on helping students ask questions, practise with quizzes, stay organized, and support one another academically.

The project is student-led. Its direction and core development came from the student creator, with AI used as a support tool during selected stages of planning, debugging, refinement, and deployment preparation. The contributions are described transparently below.

---

## Contents

- [Why Newton's Nerds?](#why-newtons-nerds)
- [Features](#features)
- [Technology](#technology)
- [How the project was built](#how-the-project-was-built)
- [Project structure and main routes](#project-structure-and-main-routes)
- [Setup](#setup)
- [Configuration](#configuration)
- [Google integrations](#google-integrations)
- [Deployment](#deployment)
- [Current status](#current-status)
- [Security notes](#security-notes)
- [Future improvements](#future-improvements)
- [Credits and acknowledgements](#credits-and-acknowledgements)

## Why Newton's Nerds?

Students often rely on several separate tools for timetables, resources, quizzes, questions, and announcements. Newton's Nerds was conceived as a single place to bring these study activities together.

The goal is not only to help students perform better in school, but also to encourage discipline, curiosity, service, and a supportive learning community. Its Christian identity is reflected in its motto and its emphasis on helping one another.

## Features

The application has been designed around the following learning workflows:

- **Quiz practice:** access quiz material connected to Google Forms, allowing students to practise and check their understanding.
- **Study timetable:** present timetable information maintained in Google Sheets, including day, time, subject, facilitator, link, location, and notes.
- **Study content and resources:** provide a central place for learning materials and subject resources.
- **Question sharing:** organize student questions by subject, with fields for the student's name, question, attempt, and timestamp.
- **Subject organization:** categories include Mathematics, Further Mathematics, Biology, Chemistry, Physics, Computer Science, Coding, and Other.
- **Motivation and reflection:** display motivational content, prayers, and Bible verses stored in Google Drive.
- **Useful forms and community information:** surface relevant forms and present group rules before students follow community-join links.
- **Administrative tools:** provide pages for managing content and questions.
- **Coding and mathematics sections:** give students dedicated entry points for these subjects.
- **Optional web-push notifications:** the codebase has included an approach using VAPID and `pywebpush` for question-related notifications.

**Important:** These describe the project's intended or implemented modules, not a guarantee that every module is currently available in every deployment. See [Current status](#current-status) for the known limitation.

## Technology

Newton's Nerds is built around a lightweight Python web stack:

| Technology | Role |
|---|---|
| Python | Main application language |
| Flask | Web application and routes |
| HTML, CSS, JavaScript | Page structure, styling, and browser interactions |
| Bootstrap / Bootswatch Flatly | Responsive layout and interface styling |
| Google Sheets with `gspread` | Structured data such as timetable and question records |
| Google Drive integrations | Resource and media discovery |
| Google Forms | Quiz and form workflows |
| `requests` | HTTP requests to external services where needed |
| `pywebpush` and VAPID | Optional web-push notification functionality |
| Gunicorn | Production WSGI server where configured |
| Render | Hosting/deployment platform used during development |

The exact dependencies and integrations enabled in a deployment are determined by its current code and configuration.

## How the project was built

Newton's Nerds is a **student-led project**. AI support was used as a development aid; it should not be confused with ownership of the idea or the overall project.

### Work led and completed by the student creator

- **Original concept and brainstorming:** identified the need for a shared study hub and developed the project's educational purpose and direction.
- **Feature planning:** decided which student needs to prioritize, including quizzes, study resources, timetable access, questions, subject organization, and community information.
- **`drive.py`:** written by the student creator from scratch, including the project's Drive-related integration work.
- **`sheets.py`:** written by the student creator from scratch, including the project's Sheets-related integration work.
- **Project ownership and decisions:** directed the product, chose what to build, and remained responsible for evaluating changes and deciding which ideas belonged in the application.
- **Project context and testing:** supplied the requirements and real-world context for a study hub intended for students, then assessed the application during development and deployment.

### How AI assisted

AI was used as a tool during selected development iterations to support work such as:

- discussing implementation options and helping reason through architecture;
- explaining code and helping investigate bugs;
- suggesting improvements to selected routes, templates, and feature integrations;
- helping refine usability, responsiveness, and presentation;
- advising on configuration and deployment-readiness, including environment variables and hosting considerations; and
- drafting or refining technical documentation.

The exact AI contribution varied by iteration. This README does not imply that AI independently created the project, wrote `drive.py` or `sheets.py`, or authored every part of the codebase. The student creator remains the project's owner and the source of its core idea and direction.

## Project structure and main routes

The application has used a Flask route structure with areas such as:

| Route | Intended purpose |
|---|---|
| `/` and `/home` | Home or study hub landing page |
| `/content` | Study content and resources |
| `/questions` | Student questions workflow |
| `/rules` | Group rules and community-join gate |
| `/admin` | Administrative area |
| `/admin/questions` | Question administration |
| `/coding` | Coding resources or section |
| `/maths` | Mathematics resources or section |
| `/health` | Health/status-related route |
| `/drive-image/<file_id>` | Serve or retrieve a Drive image through the application |

Routes and responsibilities may differ between branches or deployments. Check the current source before treating this table as a definitive API contract.

## Setup

These steps are a general setup guide. The exact start command depends on the name of the Flask entry-point file and the application object in the current repository.

### 1. Get the source code

Clone the repository, then enter its directory:

```bash
git clone <repository-url>
cd <repository-directory>
```

Replace the placeholders with the actual repository URL and directory name.

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it on Linux or macOS:

```bash
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

If the repository contains `requirements.txt`:

```bash
pip install -r requirements.txt
```

If it does not, install the project's declared dependencies from its package/dependency file. Avoid guessing dependency versions; use the versions tested with the current source.

### 4. Configure environment variables

Create a local `.env` file if the application uses `python-dotenv`, or set the variables in your shell or deployment dashboard. Use the names expected by the current code. See [Configuration](#configuration).

### 5. Run the application

Use the repository's configured entry point. Common Flask options include:

```bash
python app.py
```

or, when the Flask application object is named `app` in `app.py`:

```bash
gunicorn app:app
```

If the entry-point file or application-object name differs, adjust the command accordingly.

## Configuration

The application has used environment variables for Google resource identifiers, optional notification settings, and local database configuration. The following names have been used by the project; confirm each one against the current source before deploying.

| Variable | Purpose |
|---|---|
| `TIMETABLE_SHEET_KEY` | Identifies the timetable Google Sheet |
| `QUESTIONS_SHEET_KEY` | Identifies the question records Google Sheet |
| `GROUP_RULES_SHEET_KEY` | Identifies the rules/group information sheet |
| `QUESTIONS_SUBSCRIPTION_KEY` | Question-notification/subscription-related configuration |
| `DRIVE_PARENT_FOLDER_ID` | Parent Google Drive folder for resources or media |
| `FORMS_FOLDER_ID` | Google Drive folder used to locate forms |
| `NN_LOCAL_DB` | Local database path/configuration, if that option is enabled |
| VAPID-related variables | Public/private key and related details for web-push notifications, if enabled |
| Community-links configuration | JSON or other configuration for group/community URLs, if enabled |
| Google service-account credential setting | Path to a service-account JSON file or encoded credentials, depending on the deployment configuration |

Do not assume that every variable is required in every environment. The code may provide defaults for some features, while others will fail or be disabled if their configuration is absent.

## Google integrations

### Google Sheets

Sheets are used for structured records, including timetable entries and student questions. Access requires a Google service account or another credential method supported by the code. The relevant spreadsheets must be shared with the service-account identity with the minimum permissions needed by the application.

### Google Drive

Drive is used to discover and serve learning or motivational media. Folder IDs and permissions must match the current configuration. Nested folders, file visibility, and the way media URLs are handled can affect whether images appear in the application.

### Google Forms

Forms support quiz-related workflows. Confirm that the Forms are published/shared as intended and that the integration reads the correct folder or links. Google Forms access permissions and the application's discovery logic are separate concerns.

## Deployment

Newton's Nerds has been prepared for hosting on a platform such as Render using a WSGI server such as Gunicorn. A deployment normally needs:

1. the correct repository and branch;
2. a build/install command matching the project's dependency files;
3. a start command matching the real Flask entry point;
4. all required environment variables configured in the hosting dashboard;
5. valid Google credentials and the appropriate Sheet/Drive permissions; and
6. a deployment smoke test covering the functions that are expected to be live.

Never assume a deployment is healthy simply because the homepage loads. Test each feature against its underlying service and review the application logs when an integration fails.

## Current status

**Known status at the time this README was prepared:** the quiz feature is the only feature currently confirmed as working in the deployment. Other modules are part of the project's design/code history, but their live status should be tested before they are described as operational to users.

A useful release checklist is:

- [ ] Home page and navigation load correctly on mobile and desktop.
- [ ] Quiz links/forms load and behave as expected.
- [ ] Timetable records load from the configured Sheet.
- [ ] Question submission, filtering, and administration work, if enabled.
- [ ] Drive images load from the expected folders, including nested folders where required.
- [ ] Group rules are displayed before community-join links are opened.
- [ ] Admin routes are protected against unauthorized access.
- [ ] Push notifications work only when fully configured and permission has been granted.
- [ ] Production logs contain no unresolved startup or integration errors.

Update this section whenever a feature has been retested on the deployment being documented.

## Security notes

- **Never commit secrets.** Keep `.env`, service-account credentials, private VAPID keys, tokens, and other secrets out of Git.
- Add secret and credential files to `.gitignore` where appropriate.
- If a credential is accidentally committed or shared, revoke/rotate it rather than relying only on deleting the file from the latest commit.
- Give Google service accounts only the permissions they need and share only the relevant Sheets and Drive folders with them.
- Protect administrative endpoints with authentication and authorization checks before exposing the application publicly.
- Validate and sanitize user-submitted content, and avoid displaying private data in logs or public pages.
- Keep production debugging disabled and store secrets in the hosting provider's secret/environment-variable settings.

## Future improvements

Potential next steps include:

- restore and verify the complete end-to-end functionality of each module;
- improve the mobile experience, accessibility, and contrast across themes;
- make Drive media discovery more reliable for nested folders;
- improve the reliability of quiz/form discovery;
- strengthen admin authentication, validation, and error handling;
- add tests for the main routes and Google integrations; and
- document a verified release and deployment process.

## Credits and acknowledgements

Created and led by a student with the aim of making learning more accessible and collaborative. AI tools supported selected parts of development through brainstorming, code explanation, troubleshooting, refinement, and documentation. The project’s concept, direction, and the student-written `drive.py` and `sheets.py` are attributed to the student creator.

**Newton's Nerds — Study Hard, Pray Harder.** ❤️
