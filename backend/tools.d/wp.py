"""wp — the single boards-WordPress write surface (ada-tool-consolidation
merge-overlapping-615ba9, 2026-11-24).

Audit result: cms_publish_page/cms_edit write the MDDB miniapp CMS
(ada-cms-pages -> surf-thailand.com static lane) — a DIFFERENT sink, so
they stay separate. The real overlap was on the boards WP side: four
tools.d writers all hit the same backend door (wp_client ->
boards.surf-thailand.com REST) and differed only in WP post type:

    wp_post       -> post type 'post'    (free-form member post)
    cpt_post      -> CPTs expense/contact/reminder/asset (kind=)
    place_report  -> post type 'place'   (maps-link report page)
    wp_chat_page  -> WP 'page' under /chat/ (member webchat embed)

They consolidate into wp(action=...) per the tool-consolidation spec —
one canonical write tool per sink. The four modules stay in tools.d/ as
implementation seats (not manifest-declared); this file dispatches on
action= and delegates. The absorbed names live on as
_tool_runner._ALIASES rows for one release.

Env (wp-boards.env on the backend host): same as the absorbed tools —
    WP_SITE_URL, WP_APP_USER, WP_APP_PASSWORD
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

DECLARATION = {
    "name": "wp",
    "description": (
        "Boards WordPress writer (boards.surf-thailand.com) — action="
        "'post' member post, 'record' expense/contact/reminder/asset "
        "(kind=), 'place' maps-link report, 'chat_page' webchat page. "
        "Miniapp CMS pages go to cms_publish_page/cms_edit."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["post", "record", "place", "chat_page"],
                "description": "Which boards write: post | record | "
                               "place | chat_page.",
            },
            # -- action='post' (was wp_post) --
            "person": {
                "type": "string",
                "description": "post/record: member page or record "
                               "owner — tony, kk, or nhong. post "
                               "requires it; record defaults to the "
                               "lane's own member (saved_by meta).",
            },
            "title": {
                "type": "string",
                "description": "post: post title. record: name/subject "
                               "(contact name, reminder text, asset "
                               "name) — optional for expense, the tool "
                               "composes vendor+date.",
            },
            "name": {
                "type": "string",
                "description": "record: alias for title (same slot).",
            },
            "content": {
                "type": "string",
                "description": "post: post body text (Thai ok). record: "
                               "optional extra detail for the record "
                               "body.",
            },
            "publish": {
                "type": "boolean",
                "description": "post: publish immediately (default "
                               "true); false creates a preview draft.",
            },
            "media_url": {
                "type": "string",
                "description": "post/record: optional "
                               "/relay-img/arch/... URL of a photo just "
                               "sent — embedded at the top of the post "
                               "or attached as the record's image.",
            },
            # -- action='record' (was cpt_post) --
            "kind": {
                "type": "string",
                "enum": ["expense", "contact", "reminder", "asset"],
                "description": "record: record type — receipt/expense "
                               "photo -> expense, 'save this contact' "
                               "-> contact, 'remind us' -> reminder, "
                               "'asset: X' -> asset. Required for "
                               "action='record'.",
            },
            "status": {
                "type": "string",
                "enum": ["private", "publish", "draft"],
                "description": "record/chat_page: visibility (record "
                               "default 'private' — member login "
                               "required; chat_page default 'private').",
            },
            "amount": {
                "type": "string",
                "description": "record expense: amount paid, e.g. '320' "
                               "or '320.50'. Read it off the receipt.",
            },
            "currency": {
                "type": "string",
                "description": "record expense: ISO code (default THB).",
            },
            "vendor": {
                "type": "string",
                "description": "record expense/asset: shop or supplier "
                               "name from the receipt/manual.",
            },
            "paid_by": {
                "type": "string",
                "description": "record expense: who paid (tony|kk|"
                               "nhong) — defaults to nobody; set when "
                               "known.",
            },
            "receipt_date": {
                "type": "string",
                "description": "record expense: date on the receipt "
                               "(YYYY-MM-DD preferred).",
            },
            "phone": {
                "type": "string",
                "description": "record contact: phone number.",
            },
            "line_id": {
                "type": "string",
                "description": "record contact: LINE id.",
            },
            "role": {
                "type": "string",
                "description": "record contact: what they do — "
                               "'plumber', 'Honda service', 'insurance'.",
            },
            "due_date": {
                "type": "string",
                "description": "record reminder: when it's due "
                               "(YYYY-MM-DD preferred, time ok).",
            },
            "assignee": {
                "type": "string",
                "description": "record reminder: who it's for "
                               "(tony|kk|nhong).",
            },
            "model": {
                "type": "string",
                "description": "record asset: model name/number, e.g. "
                               "'CR-V 2.0 EL'.",
            },
            "serial": {
                "type": "string",
                "description": "record asset: serial number.",
            },
            "purchase_date": {
                "type": "string",
                "description": "record asset: purchase date "
                               "(YYYY-MM-DD).",
            },
            "warranty_until": {
                "type": "string",
                "description": "record asset: warranty expiry "
                               "(YYYY-MM-DD).",
            },
            "slug": {
                "type": "string",
                "description": "record: explicit record slug (default "
                               "derived from kind+name — contact/asset "
                               "upsert by it). chat_page: page slug under "
                               "the parent (default 'web-<member>').",
            },
            # -- action='place' (was place_report) --
            "url": {
                "type": "string",
                "description": "place: the Google Maps link the user "
                               "sent (maps.app.goo.gl short links "
                               "expand). Omit when only a place name is "
                               "known.",
            },
            "place": {
                "type": "string",
                "description": "place: place name/address to look up "
                               "when no maps URL was given.",
            },
            "ask": {
                "type": "string",
                "description": "place: what the user wants to know, "
                               "verbatim — recorded on the page as the "
                               "question.",
            },
            "lang": {
                "type": "string",
                "description": "place: page language 'th' or 'en' "
                               "(default th for kk lane chats).",
            },
            # -- action='chat_page' (was wp_chat_page) --
            "member": {
                "type": "string",
                "description": "chat_page: member slug (tony, kk, "
                               "nhong) — names the page and its "
                               "web-<member> chat_id for pushes.",
            },
            "lane": {
                "type": "string",
                "description": "chat_page: public base URL of the "
                               "member's Ada lane, e.g. "
                               "https://ada-ha.surf-thailand.com — "
                               "hosts /ws and the card JS.",
            },
            "parent": {
                "type": "string",
                "description": "chat_page: parent page slug (default "
                               "'chat').",
            },
        },
        "required": ["action"],
    },
}

# action= -> absorbed implementation module (same dir, not manifest-
# declared — the absorbed names' DECLARATIONs stay off-surface).
_ACTION_IMPL = {
    "post": "wp_post",
    "record": "cpt_post",
    "place": "place_report",
    "chat_page": "wp_chat_page",
}

_IMPL_MODULES: dict[str, Any] = {}


def _load_impl(name: str) -> Any:
    """Load a sibling tools.d module by path — tools.d isn't a package,
    so this mirrors tools_loader's spec_from_file_location pattern."""
    mod = _IMPL_MODULES.get(name)
    if mod is None:
        path = Path(__file__).resolve().with_name(f"{name}.py")
        spec = importlib.util.spec_from_file_location(
            f"tools_d.{name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _IMPL_MODULES[name] = mod
    return mod


async def run(runner: Any, **args: Any) -> dict:
    action = str(args.get("action") or "").strip().lower()
    impl_name = _ACTION_IMPL.get(action)
    if impl_name is None:
        return {"ok": False,
                "error": f"wp action must be one of "
                         f"{sorted(_ACTION_IMPL)} — got {action!r}"}
    # 'action' is the dispatch key, not an impl arg — cpt_post would
    # otherwise report it back as an ignored meta field. Everything
    # else passes through verbatim: each impl reads only its own keys,
    # exactly as it did as a standalone tool.
    call_args = {k: v for k, v in args.items() if k != "action"}
    mod = _load_impl(impl_name)
    return await mod.run(runner, **call_args)
