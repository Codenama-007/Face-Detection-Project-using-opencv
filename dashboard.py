import streamlit as st
import json
import time
import os

st.set_page_config(page_title="Exam Dashboard", layout="wide")

st.title("🎓 AI Exam Monitoring Dashboard")

placeholder = st.empty()


def read_snapshot():
    try:
        file_path = "snapshot_tmp.json"

        if not os.path.exists(file_path):
            return None

        with open(file_path, "r") as f:
            content = f.read().strip()

        if not content:
            return None

        data = json.loads(content)

        if not isinstance(data, dict):
            return None

        return data

    except json.JSONDecodeError:
        return None
    except Exception:
        return None


while True:

    data = read_snapshot()

    with placeholder.container():

        if not data:
            st.warning("Waiting for valid data...")
        else:
            col1, col2, col3 = st.columns(3)

            col1.metric("Student ID", data.get("id", "NA"))
            col2.metric("Risk Score", data.get("risk_score", 0))
            col3.metric("Direction", data.get("direction", "UNKNOWN"))

            status = data.get("status", "UNKNOWN")

            if status == "NORMAL":
                st.success(status)
            elif status == "SUSPICIOUS":
                st.warning(status)
            else:
                st.error(status)

    time.sleep(1)