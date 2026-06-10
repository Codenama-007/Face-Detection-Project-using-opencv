# ProctorAI - AI Examination Integrity Platform

## Overview
ProctorAI is an advanced AI-powered exam monitoring system designed to ensure academic integrity during remote examinations. By utilizing computer vision and machine learning, it continuously tracks student behavior in real-time, calculates risk scores, and provides a clear status (Normal, Suspicious, or High Risk) to exam administrators.

## Features
- **Real-Time Head Pose & Gaze Tracking**: Uses MediaPipe to monitor face orientation (Left, Right, Top, Bottom, Center).
- **Risk Scoring System**: Automatically accrues risk points if a student looks away from the screen or if no face is detected for extended periods.
- **Live Status Monitoring**: Evaluates risk scores to categorize the student's status dynamically.
- **Streamlit Dashboard**: A fast and lightweight dashboard to view real-time metrics of the test taker.
- **Web Platform Interface**: A beautifully designed enterprise suite containing a Landing Page, Live Monitoring Dashboard, Reports & Analytics, and an Exam Replay Engine.

## Project Structure

### 1. Python Backend & Core Logic
- **`main.py`**: The core monitoring engine. It captures the live webcam feed, processes frames via MediaPipe Face Detection, calculates risk scores based on face orientation and absence duration, and exports the state continuously to a JSON file (`snapshot_tmp.json`).
- **`dashboard.py`**: A Streamlit application that reads the `snapshot_tmp.json` state and provides a live UI displaying the student's ID, Risk Score, Direction, and overall Status.
- **`app.py`**: A simpler, legacy script utilizing OpenCV's Haarcascades for detecting faces in static images stored in an `images/` directory.
- **`test.py`**: A utility script for testing face detection components.

### 2. Frontend Enterprise Web Suite
A complete HTML/CSS/JS frontend to provide a polished UX for administrators:
- **`index.html`** / **`landing.js`** / **`landing.css`**: The modern landing page introducing ProctorAI features.
- **`monitoring.html`** / **`main.js`**: The live monitoring dashboard interface.
- **`reports.html`** / **`reports.js`** / **`reports.css`**: Interface for exam analytics and risk insights.
- **`replay.html`** / **`replay.js`** / **`replay.css`**: An interface simulating an exam "flight recorder" to review flagged incidents.
- **`style.css`**: Global styles used across the platform.

## How it Works
1. **Detection Engine**: Running `main.py` activates the webcam. It maps the bounding box of the face and determines where the user is looking.
2. **Risk Accumulation**: Looking away from the center accumulates risk over time. If no face is detected for more than 2 seconds, the risk score spikes.
3. **Status Classification**:
   - `Risk < 10`: **NORMAL**
   - `10 <= Risk < 30`: **SUSPICIOUS**
   - `Risk >= 30`: **HIGH RISK**
4. **Dashboard View**: The Streamlit dashboard (`dashboard.py`) or the web UI can be used by an invigilator to monitor these metrics in real-time.

## Installation & Setup

1. **Install Dependencies**
   Make sure you have Python installed, then run:
   ```bash
   pip install -r requirements.txt
   ```
   *Primary dependencies include OpenCV (`cv2`), MediaPipe, and Streamlit.*

2. **Run the AI Monitoring Engine**
   Start the core vision engine to begin tracking:
   ```bash
   python main.py
   ```
   *Press `q` on the video window to stop monitoring.*

3. **Run the Dashboard**
   In a separate terminal, launch the Streamlit dashboard:
   ```bash
   streamlit run dashboard.py
   ```

4. **Explore the Web Interface**
   Open `index.html` in your favorite web browser to explore the enterprise UI frontend.