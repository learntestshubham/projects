# LinkedIn Easy Apply Bot

Playwright-based LinkedIn Easy Apply automation built with Python.

## Features

- Persistent Chromium profile for saved LinkedIn login
- Manual login only, never automated
- Easy Apply job search flow
- Resume upload support
- Interactive handling for missing required answers
- CSV logging and applied-job tracking
- Experience-based skipping and company exclusions

## Setup

```bash
cd linkedin-auto-apply
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
cd linkedin-auto-apply
. .venv/bin/activate
python3 linkedin_easy_apply_bot.py
```

## Privacy

This repository excludes local browser profiles, runtime logs, saved answers, and other personal state through `.gitignore`.
