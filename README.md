## NOTICE THIS VERSION IS EXPERIENCIENG A MAX COOKIE CACHE ERROR CAUSING IT TO STOP ASKING QUESTIONS PAST 68, A FIX IS BEING ROLLED OUT SOON

# RSK HAM Study

RSK HAM Study is a web‑based training tool for anyone preparing for the Kenyan amateur radio (HAM) certification. It offers both **Study** and **Exam** modes so you can learn at your own pace or simulate the timed, multiple‑choice test environment.

## Live demo

Visit the running application here: [https://rsk-ham-study.ew.r.appspot.com/](https://rsk-ham-study.ew.r.appspot.com/).

## Features

- **Study Mode** – cycles through all available questions until you have answered each one correctly at least once.  
- **Exam Mode** – presents a random set of 60 questions to mimic a real exam session, complete with a summary of your results at the end.  
- **Review mistakes** – after finishing an exam you can review only the questions you answered incorrectly.  
- **Progress tracking** – your correct answers are stored in your browser so you can pick up where you left off.  
- **Responsive design** – works on both desktop and mobile devices.

> **Note**: Questions that require drawings or symbols (sections 5 and 6 of the official syllabus) are not included in the current version.

## Getting started

These instructions assume you have Python 3.8+ installed. The app is built with [Flask](https://flask.palletsprojects.com/) and uses [Jinja](https://jinja.palletsprojects.com/) for templating.

### Clone the repository

```bash
git clone https://github.com/lucasLB7/rskhamstudy.git
cd rskhamstudy
