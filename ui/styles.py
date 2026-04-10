from __future__ import annotations

import streamlit as st


APP_CSS = """
<style>
.block-container {
    padding-top: 1.35rem;
}
[data-testid="stSidebarContent"] {
    background:
        radial-gradient(120% 65% at 0% 0%, #e8f2ff 0%, rgba(232, 242, 255, 0) 60%),
        linear-gradient(180deg, #f8fbff 0%, #eef3f9 100%);
    border-right: 1px solid #dbe3ef;
}
[data-testid="stChatMessage"] {
    align-items: center;
}
[data-testid="stChatMessage"] [data-testid="stChatMessageAvatarAssistant"],
[data-testid="stChatMessage"] [data-testid="stChatMessageAvatarUser"] {
    align-self: center;
    margin-top: 0;
}
[data-testid="stChatMessage"] [data-testid="stChatMessageContent"] {
    align-self: center;
}
[data-testid="stBottomBlockContainer"] {
    padding-right: 7.2rem;
}
[data-testid="stExpander"] details {
    border-radius: 10px;
}
div[class*="st-key-taskcard_"] {
    position: relative;
    border: 1px solid #d4ddec;
    border-radius: 16px;
    padding: 0.62rem 0.68rem 0.58rem;
    margin-bottom: 0.58rem;
    background: linear-gradient(145deg, #ffffff 0%, #f7fbff 100%);
    box-shadow: 0 12px 24px -22px #1f2937;
    transition: transform 140ms ease, box-shadow 140ms ease, border-color 140ms ease;
}
div[class*="st-key-taskcard_active_"] {
    border-color: #c7d9f4;
    background: linear-gradient(145deg, #f1f7ff 0%, #e7f1ff 100%);
}
div[class*="st-key-taskcard_paused_"] {
    border-color: #ead7ba;
    background: linear-gradient(145deg, #fff8ee 0%, #fff1df 100%);
}
div[class*="st-key-taskgroup_"] [data-testid="stExpander"] details {
    border: 1px solid #d7e1ef;
    border-radius: 10px;
    background: linear-gradient(180deg, #f9fbff 0%, #f1f6fd 100%);
    box-shadow: 0 12px 24px -26px #1f2937;
    overflow: hidden;
    margin-bottom: 0.62rem;
}
div[class*="st-key-taskgroup_"] [data-testid="stExpander"] details summary {
    padding-top: 0.16rem;
    padding-bottom: 0.16rem;
}
div[class*="st-key-taskcard_"]::before {
    content: "";
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 4px;
    border-radius: 16px 0 0 16px;
    background: linear-gradient(180deg, #2563eb 0%, #0ea5e9 45%, #10b981 100%);
}
div[class*="st-key-taskcard_"]:hover {
    transform: translateY(-1px);
    border-color: #b8c7de;
    box-shadow: 0 16px 28px -22px #1f2937;
}
div[class*="st-key-taskcard_"] [data-testid="stExpander"] details {
    border: 1px solid rgba(191, 208, 232, 0.95);
    border-radius: 10px;
    background: rgba(255, 255, 255, 0.5);
    margin-top: 0.44rem;
    overflow: hidden;
}
div[class*="st-key-taskcard_"] [data-testid="stExpander"] details summary {
    padding-top: 0.02rem;
    padding-bottom: 0.02rem;
}
div[class*="st-key-taskcard_"] [data-testid="stExpander"] details[open] {
    background: rgba(255, 255, 255, 0.74);
}
.task-badge {
    font-size: 0.67rem;
    font-weight: 700;
    padding: 0.1rem 0.42rem;
    border-radius: 999px;
    border: 1px solid transparent;
    line-height: 1.15;
    white-space: nowrap;
}
.task-badge-active {
    background: #e7f8ef;
    border-color: #bde5cb;
    color: #116432;
}
.task-badge-paused {
    background: #fff4e5;
    border-color: #ffd9a8;
    color: #8a4b00;
}
.task-row {
    font-size: 0.82rem;
    color: #273449;
    margin: 0.08rem 0;
    line-height: 1.28;
}
.task-title {
    margin: 0;
    font-size: 0.94rem;
    color: #0f2138;
    line-height: 1.22;
}
.task-title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.45rem;
    margin: 0.04rem 0 0.18rem;
}
.task-row-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.55rem;
    flex-wrap: wrap;
}
.task-row strong {
    color: #132238;
    font-weight: 700;
}
.task-prompt {
    margin-top: 0.28rem;
    padding: 0.02rem 0 0.18rem;
    font-size: 0.88rem;
    color: #111f33;
    line-height: 1.34;
}
.task-details-label {
    font-size: 0.76rem;
    color: #3d5a83;
    font-weight: 700;
    letter-spacing: 0.01em;
    text-transform: uppercase;
    margin-bottom: 0.18rem;
}
.empty-chat-subtitle {
    margin-top: -0.35rem;
    margin-bottom: 0.85rem;
    font-size: 1rem;
    color: #4b6382;
    font-weight: 600;
    letter-spacing: 0.01em;
}
.task-topline {
    display: flex;
    align-items: center;
    gap: 0.3rem;
}
div[class*="st-key-delete_task_"] button {
    border-radius: 10px;
    border: 1px solid #f2c3c3;
    background: #fff5f5;
    color: #b42318;
    min-height: 2rem;
    font-weight: 700;
    white-space: nowrap;
}
div[class*="st-key-delete_task_"] button:hover {
    border-color: #e59a9a;
    background: #ffe8e8;
    color: #931f16;
}
div[class*="st-key-pause_task_"] button {
    border-radius: 10px;
    border: 1px solid #ffd8b3;
    background: #fff6eb;
    color: #a04a00;
    min-height: 2rem;
    font-weight: 700;
    white-space: nowrap;
}
div[class*="st-key-pause_task_"] button:hover {
    border-color: #ffc68f;
    background: #ffedd9;
}
div[class*="st-key-resume_task_"] button {
    border-radius: 10px;
    border: 1px solid #b9e5c8;
    background: #ecfbf2;
    color: #15623a;
    min-height: 2rem;
    font-weight: 700;
    white-space: nowrap;
}
div[class*="st-key-resume_task_"] button:hover {
    border-color: #9fdab5;
    background: #ddf6e8;
}
div[class*="st-key-run_task_"] button {
    border-radius: 10px;
    border: 1px solid #b7cff8;
    background: #edf4ff;
    color: #1e4ca3;
    min-height: 2rem;
    font-weight: 700;
    white-space: nowrap;
}
div[class*="st-key-run_task_"] button:hover {
    border-color: #9dbcf3;
    background: #e2eeff;
}
div[class*="st-key-save_task_"] button {
    border-radius: 10px;
    border: 1px solid #b7cff8;
    background: #edf4ff;
    color: #1e4ca3;
    min-height: 2rem;
    font-weight: 700;
}
div[class*="st-key-save_task_"] button:hover {
    border-color: #9dbcf3;
    background: #e2eeff;
}
div[class*="st-key-edit_task_"] button {
    border-radius: 10px;
    border: 1px solid #c6d8f7;
    background: #f0f6ff;
    color: #2451a6;
    min-height: 2rem;
    font-weight: 700;
}
div[class*="st-key-edit_task_"] button:hover {
    border-color: #a9c5f0;
    background: #e6f0ff;
}
div[class*="st-key-cancel_task_edit_"] button {
    border-radius: 10px;
    border: 1px solid #d7dfeb;
    background: #f7f9fc;
    color: #334155;
    min-height: 2rem;
    font-weight: 700;
}
div[class*="st-key-cancel_task_edit_"] button:hover {
    border-color: #c4d0e2;
    background: #f0f4f9;
}
div[class*="st-key-scheduled_result_"] [data-testid="stExpander"] details {
    border: 1px solid #d4ddec;
    border-radius: 10px;
    background: linear-gradient(145deg, #ffffff 0%, #f8fbff 100%);
    box-shadow: 0 12px 24px -22px #1f2937;
    overflow: hidden;
}
div[class*="st-key-scheduled_result_"] [data-testid="stExpander"] details summary {
    padding-top: 0.12rem;
    padding-bottom: 0.12rem;
}
div[class*="st-key-scheduled_result_read_"] [data-testid="stExpander"] details:not([open]) {
    border-color: #d5dfec;
    background: #f1f6fb;
}
div[class*="st-key-scheduled_result_unread_"] [data-testid="stExpander"] details:not([open]) {
    border-color: #8ab4ff;
    background:
        radial-gradient(140% 120% at 0% 0%, rgba(96, 165, 250, 0.22) 0%, rgba(96, 165, 250, 0) 58%),
        linear-gradient(145deg, #eff6ff 0%, #dbeafe 100%);
    box-shadow: 0 16px 30px -24px #2563eb;
}
div[class*="st-key-scheduled_result_"] [data-testid="stExpander"] details[open] {
    border-color: #bfd0e8;
    background: linear-gradient(180deg, #ffffff 0%, #fbfdff 100%);
}
div[class*="st-key-mark_scheduled_read_"] button {
    border-radius: 10px;
    border: 1px solid #b7cff8;
    background: #edf4ff;
    color: #1e4ca3;
    min-height: 2rem;
    font-weight: 700;
}
div[class*="st-key-mark_scheduled_read_"] button:hover {
    border-color: #9dbcf3;
    background: #e2eeff;
}
div[class*="st-key-task_prompt_draft_"] textarea {
    border-radius: 12px;
    border: 1px solid #d3dded;
    background: #fbfdff;
    color: #10233b;
    line-height: 1.35;
}
div[class*="st-key-clear_chat_bottom_shell"] {
    position: fixed;
    right: 1rem;
    bottom: 3.35rem;
    z-index: 1000;
    width: 5.4rem;
}
div[class*="st-key-clear_chat_main_"] button {
    border-radius: 10px;
    border: 1px solid #b9cff5;
    background: linear-gradient(180deg, #eaf3ff 0%, #dceafe 100%);
    color: #214b90;
    min-height: 2.55rem;
    padding: 0.18rem 0.7rem;
    font-weight: 700;
    box-shadow: 0 10px 20px -18px #1f2937;
    white-space: nowrap;
}
div[class*="st-key-clear_chat_main_"] button:hover {
    border-color: #9ebcf0;
    background: linear-gradient(180deg, #deecff 0%, #cfdef8 100%);
    color: #173e7c;
}
@media (max-width: 768px) {
    [data-testid="stBottomBlockContainer"] {
        padding-right: 6.1rem;
    }
    div[class*="st-key-clear_chat_bottom_shell"] {
        right: 0.65rem;
        bottom: 2.85rem;
        width: 4.7rem;
    }
    div[class*="st-key-taskcard_"] {
        padding: 0.58rem 0.62rem 0.54rem;
        margin-bottom: 0.54rem;
    }
    div[class*="st-key-taskcard_"]:hover {
        transform: none;
    }
    .task-row {
        font-size: 0.8rem;
    }
    .task-prompt {
        font-size: 0.85rem;
    }
}
</style>
"""


def apply_app_styles() -> None:
    st.markdown(APP_CSS, unsafe_allow_html=True)
