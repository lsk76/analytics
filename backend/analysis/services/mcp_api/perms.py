"""Django-права для інструментів MCP: те, що людині можна в адмінці, те саме
їй можна через MCP — і навпаки. Скоуп ролі (read/write/create/admin) — стеля
«наскільки небезпечно», право Django — «до якого розділу є доступ».

Кожен інструмент → одне право `app.codename` (як `user.has_perm`). Суперюзер
і локальний stdio проходять без перевірки. Тест `test_every_tool_has_perm`
не дасть додати інструмент без рядка тут.
"""
V_ACC, C_ACC, A_ACC = ("accounts.view_telegramaccount", "accounts.change_telegramaccount",
                       "accounts.add_telegramaccount")
V_TASK = "analysis.view_analysistask"

PERMS = {
    # --- сервіс
    "tools_manifest": "", "service_health": V_TASK, "service_queues": V_TASK,
    "settings_list": "analysis.view_setting", "setting_show": "analysis.view_setting",
    "setting_set": "analysis.change_setting",
    "publish_status": "analysis.view_publishconfig",
    "publish_config_show": "analysis.view_publishconfig",
    "publish_config_create": "analysis.add_publishconfig",
    "publish_config_update": "analysis.change_publishconfig",
    "published_list": "analysis.view_publishedevent", "published_show": "analysis.view_publishedevent",
    # --- акаунти й проксі (проксі не мають окремої групи — йдуть за акаунтами)
    "accounts_list": V_ACC, "account_show": V_ACC, "account_check": V_ACC, "account_dialogs": V_ACC,
    "account_jobs": "accounts.view_warmupjob",
    "account_repair": C_ACC, "account_spam_check": C_ACC, "account_update": C_ACC,
    "account_warm_up": C_ACC, "account_import": A_ACC, "account_import_batch": A_ACC,
    "proxies_list": "accounts.view_proxy", "proxy_check": "accounts.change_proxy",
    # --- задачі, збори, чати, джерела, події, довідник
    "tasks_list": V_TASK, "task_show": V_TASK, "task_update": "analysis.change_analysistask",
    "prompt_try": "analysis.change_analysistask", "posts_retag": "analysis.change_event",
    "task_create": "analysis.add_analysistask",
    "runs_list": "analysis.view_researchrun", "run_show": "analysis.view_researchrun",
    "run_create": "analysis.add_researchrun", "run_cancel": "analysis.change_researchrun",
    "chats_list": "analysis.view_monitorchat", "chat_update": "analysis.change_monitorchat",
    "chat_add": "analysis.add_monitorchat", "chat_delete": "analysis.delete_monitorchat",
    "rubrics_list": "analysis.view_researchrubric",
    "rubric_create": "analysis.add_researchrubric",
    "rubric_update": "analysis.change_researchrubric",
    "rubric_delete": "analysis.delete_researchrubric",
    "sources_list": "analysis.view_source", "source_update": "analysis.change_source",
    "source_add": "analysis.add_source", "source_subscribe": "analysis.add_sourcesubscription",
    "events_stats": "analysis.view_event", "events_list": "analysis.view_event",
    "event_show": "analysis.view_event", "event_update": "analysis.change_event",
    "event_add": "analysis.add_event", "tag_categories": "analysis.view_tagcategory",
    "tag_category_show": "analysis.view_tagcategory",
    "tag_category_create": "analysis.add_tagcategory",
    "tag_category_update": "analysis.change_tagcategory",
    "tag_category_delete": "analysis.delete_tagcategory",
    "tags_list": "analysis.view_tag", "tag_show": "analysis.view_tag",
    "tag_create": "analysis.add_tag", "tag_update": "analysis.change_tag",
    "tag_delete": "analysis.delete_tag",
    "channels_find": "analysis.view_channel", "channel_add": "analysis.add_channel",
    "channel_update": "analysis.change_channel",
    # --- TeleZip: розвідка для задач
    "tz_status": V_TASK, "tz_find": V_TASK, "tz_channels": V_TASK, "tz_users": V_TASK,
}

# --- Telegram «руками» акаунтів: читання = бачити акаунти, дії = правити їх
_TG_READ = ("tg_dialogs", "tg_chat_info", "tg_history", "tg_messages", "tg_context",
            "tg_search_global", "tg_message_link", "tg_download", "tg_participants",
            "tg_common_chats", "tg_topics", "tg_contacts", "tg_contacts_search",
            "tg_user_photos", "tg_privacy", "tg_folders", "tg_drafts")
_TG_WRITE = ("tg_send", "tg_edit", "tg_delete", "tg_forward", "tg_pin", "tg_mark_read", "tg_react",
             "tg_poll", "tg_click", "tg_send_file", "tg_join", "tg_leave", "tg_create_chat",
             "tg_invite", "tg_kick", "tg_ban", "tg_restrict", "tg_admin", "tg_invite_link",
             "tg_edit_chat", "tg_contact_add", "tg_contact_delete", "tg_block",
             "tg_update_profile", "tg_set_photo", "tg_draft")
PERMS.update({n: V_ACC for n in _TG_READ})
PERMS.update({n: C_ACC for n in _TG_WRITE})


def required(tool_name: str) -> str:
    return PERMS.get(tool_name, "")


def label(perm: str) -> str:
    """«analysis.add_source» → «Джерело: додати» — для повідомлення про відмову."""
    from django.contrib.auth.models import Permission
    try:
        app, codename = perm.split(".", 1)
        p = Permission.objects.select_related("content_type").get(
            content_type__app_label=app, codename=codename)
        action = {"view": "переглядати", "add": "додавати", "change": "змінювати",
                  "delete": "видаляти"}.get(codename.split("_", 1)[0], codename)
        return f"{p.content_type.name}: {action}"
    except Exception:  # noqa: BLE001
        return perm
