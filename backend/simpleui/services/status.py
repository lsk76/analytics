"""Перенесено в analysis.services.study_status (прототип /app/ знімається)."""
from analysis.services.study_status import *  # noqa: F401,F403
from analysis.services.study_status import LIVE, PROBLEM, STOPPED, Status, ago, task_status  # noqa: F401
