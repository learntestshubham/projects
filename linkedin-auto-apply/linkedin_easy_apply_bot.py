from __future__ import annotations

import csv
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Locator, Page, TimeoutError, sync_playwright


# =========================
# Editable Config
# =========================
@dataclass
class UserProfile:
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    years_experience: str = "5"
    linkedin_url: str = ""
    resume_pdf_path: str = ""


@dataclass
class BotConfig:
    job_title: str = "Software Engineer"
    search_job_titles: list[str] = field(
        default_factory=lambda: ["Software Engineer 2"]
    )
    location: str = "India"
    max_jobs: int = 2
    scan_links_limit: int = 30

    user_data_dir: str = "./playwright_linkedin_profile"
    applied_jobs_file: str = "./applied_jobs.json"
    results_csv: str = "./linkedin_apply_results_v2.csv"
    answer_memory_file: str = "./answer_memory.json"
    login_state_file: str = "./login_verified_once.flag"

    min_delay_sec: float = 2.0
    max_delay_sec: float = 5.0

    # Optional: set Chrome executable if needed on your machine.
    executable_path: str | None = None

    auto_submit: bool = True
    interactive_missing_data: bool = True
    require_manual_login_once: bool = True
    fill_optional_fields: bool = False
    max_allowed_years_experience: int = 7
    min_preferred_years_experience: int = 3
    excluded_companies: list[str] = field(default_factory=lambda: ["PayPal", "Freshworks"])

    profile: UserProfile = field(default_factory=UserProfile)


CONFIG = BotConfig()


# =========================
# Logging + Utilities
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def human_delay(cfg: BotConfig) -> None:
    time.sleep(random.uniform(cfg.min_delay_sec, cfg.max_delay_sec))


def safe_text(locator: Locator) -> str:
    try:
        txt = locator.inner_text(timeout=1500)
        return re.sub(r"\s+", " ", txt).strip()
    except Exception:
        return ""


def normalize_question(text: str) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def question_variants(text: str) -> list[str]:
    base = normalize_question(text)
    compact = re.sub(r"\s+", "", base)
    trimmed = re.sub(r"\b(your|please|the|a|an)\b", "", base)
    trimmed = re.sub(r"\s+", " ", trimmed).strip()

    variants: list[str] = []
    for v in [base, trimmed, compact]:
        if v and v not in variants:
            variants.append(v)
    return variants


def is_required(locator: Locator) -> bool:
    return locator.get_attribute("required") is not None or locator.get_attribute("aria-required") == "true"


def ensure_results_csv(path: Path) -> None:
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "search_title",
                "job_id",
                "job_title",
                "company",
                "status",
                "reason",
                "experience_detected_years",
                "experience_band",
                "experience_evidence",
            ],
        )
        writer.writeheader()


def append_result(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "search_title",
                "job_id",
                "job_title",
                "company",
                "status",
                "reason",
                "experience_detected_years",
                "experience_band",
                "experience_evidence",
            ],
        )
        writer.writerow(row)


def load_applied_jobs(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data)
    except Exception:
        return set()


def save_applied_jobs(path: Path, jobs: set[str]) -> None:
    path.write_text(json.dumps(sorted(jobs), indent=2), encoding="utf-8")


def default_memory() -> dict[str, Any]:
    return {
        "answers": {},
        "aliases": {},
        "resume_pdf_path": "",
    }


def memory_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_memory()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_memory()
        out = default_memory()
        out.update(data)
        if not isinstance(out.get("answers"), dict):
            out["answers"] = {}
        if not isinstance(out.get("aliases"), dict):
            out["aliases"] = {}
        return out
    except Exception:
        return default_memory()


def memory_save(path: Path, memory: dict[str, Any]) -> None:
    path.write_text(json.dumps(memory, indent=2), encoding="utf-8")


def memory_resolve_answer(memory: dict[str, Any], question: str) -> tuple[str | None, str | None]:
    answers = memory.get("answers", {})
    aliases = memory.get("aliases", {})

    for key in question_variants(question):
        if key in answers and str(answers[key]).strip():
            return str(answers[key]), key

    for key in question_variants(question):
        mapped = aliases.get(key)
        if mapped and mapped in answers and str(answers[mapped]).strip():
            return str(answers[mapped]), mapped

    return None, None


def memory_store_answer(memory: dict[str, Any], question: str, answer: str) -> None:
    canonical = normalize_question(question)
    if not canonical:
        return

    memory.setdefault("answers", {})[canonical] = answer
    memory.setdefault("aliases", {})

    for var in question_variants(question):
        memory["aliases"][var] = canonical


def prompt_and_store_missing_answer(
    prompt_label: str,
    question: str,
    memory: dict[str, Any],
    cfg: BotConfig,
) -> str | None:
    if not cfg.interactive_missing_data:
        return None

    print()
    print("Missing required Easy Apply answer")
    print(f"Type: {prompt_label}")
    print(f"Question: {question or '(unknown question label)'}")
    answer = input("Enter answer (leave blank to skip this job): ").strip()

    if not answer:
        return None

    memory_store_answer(memory, question, answer)
    return answer


def map_answer_from_profile(question: str, cfg: BotConfig) -> str | None:
    q = normalize_question(question)
    p = cfg.profile

    rules = [
        (["full name", "name"], p.full_name),
        (["email"], p.email),
        (["phone", "mobile"], p.phone),
        (["city", "location", "address"], p.location),
        (["years of experience", "experience"], p.years_experience),
        (["linkedin", "profile url"], p.linkedin_url),
        (["website", "portfolio"], p.linkedin_url),
    ]

    for keys, value in rules:
        if value and any(k in q for k in keys):
            return value.strip()

    return None


def resolve_answer(question: str, cfg: BotConfig, memory: dict[str, Any]) -> tuple[str | None, str | None]:
    from_profile = map_answer_from_profile(question, cfg)
    if from_profile:
        return from_profile, "from_config"

    from_memory, _ = memory_resolve_answer(memory, question)
    if from_memory:
        return from_memory, "from_memory"

    return None, None


# =========================
# LinkedIn Interaction
# =========================
def launch_persistent_browser(cfg: BotConfig) -> tuple[BrowserContext, Page]:
    kwargs: dict[str, Any] = {
        "user_data_dir": cfg.user_data_dir,
        "headless": False,
        "args": ["--start-maximized"],
        "viewport": {"width": 1400, "height": 900},
    }
    if cfg.executable_path:
        kwargs["executable_path"] = cfg.executable_path

    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(**kwargs)
    page = context.pages[0] if context.pages else context.new_page()

    setattr(context, "_pw", playwright)
    return context, page


def close_persistent_browser(context: BrowserContext) -> None:
    pw = getattr(context, "_pw", None)
    context.close()
    if pw:
        pw.stop()


def wait_for_manual_login(page: Page) -> None:
    login_state = Path(CONFIG.login_state_file)
    page.goto("https://www.linkedin.com/jobs/", wait_until="domcontentloaded")
    time.sleep(2)

    if CONFIG.require_manual_login_once and not login_state.exists():
        logging.info("One-time manual login checkpoint: please confirm LinkedIn login in the opened browser.")
        input("After confirming/login in browser, press Enter to continue: ")
        page.goto("https://www.linkedin.com/jobs/", wait_until="domcontentloaded")
        if "login" in page.url:
            raise RuntimeError("Still on LinkedIn login page. Please complete login and rerun.")
        login_state.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        logging.info("Manual login checkpoint completed and saved.")
        return

    current = page.url
    if "login" not in current and "checkpoint" not in current:
        logging.info("Existing LinkedIn session found. Skipping manual login.")
        return

    logging.info("Manual login required. Please log in to LinkedIn in the opened browser.")
    input("Press Enter after LinkedIn login is complete: ")

    page.goto("https://www.linkedin.com/jobs/", wait_until="domcontentloaded")
    if "login" in page.url:
        raise RuntimeError("Still not logged in. Please rerun after successful manual login.")

    logging.info("Login detected and session persisted in user_data_dir.")


def run_job_search(page: Page, cfg: BotConfig) -> None:
    query = (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={cfg.job_title.replace(' ', '%20')}"
        f"&location={cfg.location.replace(' ', '%20')}"
        "&f_AL=true"
    )
    page.goto(query, wait_until="domcontentloaded")
    human_delay(cfg)


def apply_easy_apply_filter(page: Page, cfg: BotConfig) -> None:
    easy_apply_button = page.locator(
        "button:has-text('Easy Apply'), "
        "label:has-text('Easy Apply'), "
        "div[role='button']:has-text('Easy Apply')"
    ).first

    if easy_apply_button.count() == 0:
        logging.warning("Easy Apply filter control not found. Continuing because URL includes f_AL=true.")
        return

    try:
        easy_apply_button.click(timeout=8000)
        human_delay(cfg)
    except Exception:
        logging.warning("Could not click Easy Apply filter. Continuing with f_AL=true URL filter.")


def collect_job_links(page: Page, max_jobs: int) -> list[str]:
    # LinkedIn lazy-loads more results as the list scrolls.
    scroll_containers = [
        page.locator("div.scaffold-layout__list").first,
        page.locator("div.jobs-search-results-list").first,
        page.locator("ul.jobs-search__results-list").first,
    ]
    for container in scroll_containers:
        try:
            if container.count() == 0 or not container.is_visible():
                continue
            for _ in range(8):
                container.evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
                time.sleep(0.8)
            break
        except Exception:
            continue

    anchors = page.locator(
        "li:has-text('Easy Apply') a[href*='/jobs/view/'], "
        "div.job-card-container:has-text('Easy Apply') a[href*='/jobs/view/'], "
        "li:has-text('Easy Apply') a[href*='currentJobId='], "
        "div.job-card-container:has-text('Easy Apply') a[href*='currentJobId=']"
    )
    links: list[str] = []
    seen: set[str] = set()

    total = anchors.count()
    for i in range(total):
        href = anchors.nth(i).get_attribute("href")
        if not href:
            continue
        if href.startswith("/"):
            href = f"https://www.linkedin.com{href}"
        if "currentJobId=" in href:
            match = re.search(r"currentJobId=(\d+)", href)
            if not match:
                continue
            href = f"https://www.linkedin.com/jobs/view/{match.group(1)}"
        elif "/jobs/view/" in href:
            href = href.split("?")[0]
        else:
            continue

        if href in seen:
            continue
        seen.add(href)
        links.append(href)

        if len(links) >= max_jobs:
            break

    return links


def extract_job_id_from_url(url: str) -> str:
    m = re.search(r"/view/(\d+)", url)
    return m.group(1) if m else ""


def extract_job_meta(page: Page, fallback_idx: int) -> tuple[str, str]:
    title = ""
    company = ""

    for sel in ["h1.t-24", "h1.jobs-unified-top-card__job-title", "h2.top-card-layout__title"]:
        loc = page.locator(sel).first
        if loc.count() > 0:
            title = safe_text(loc)
            if title:
                break

    for sel in [
        "div.jobs-unified-top-card__company-name a",
        "span.jobs-unified-top-card__company-name",
        "a.topcard__org-name-link",
    ]:
        loc = page.locator(sel).first
        if loc.count() > 0:
            company = safe_text(loc)
            if company:
                break

    if not title:
        title = f"Unknown Job #{fallback_idx + 1}"
    if not company:
        company = "Unknown Company"

    return title, company


def company_is_excluded(company: str, cfg: BotConfig) -> bool:
    company_norm = normalize_question(company)
    if not company_norm:
        return False
    return any(normalize_question(name) in company_norm for name in cfg.excluded_companies)


def detect_experience_requirement(page: Page, cfg: BotConfig) -> dict[str, Any]:
    text_parts: list[str] = []

    for sel in [
        ".jobs-box__html-content",
        ".jobs-description__content",
        ".jobs-description-content__text",
        ".jobs-unified-top-card__job-insight",
    ]:
        loc = page.locator(sel)
        count = min(loc.count(), 3)
        for i in range(count):
            txt = safe_text(loc.nth(i))
            if txt:
                text_parts.append(txt)

    haystack = " ".join(text_parts).lower()
    if not haystack:
        return {
            "detected_years": "",
            "experience_band": "unknown",
            "experience_evidence": "",
            "is_preferred": True,
            "skip_reason": "",
        }

    patterns = [
        r"(\d+)\s*-\s*(\d+)\s*(?:years|yrs)\s+(?:of\s+)?experience",
        r"(\d+)\s*to\s*(\d+)\s*(?:years|yrs)\s+(?:of\s+)?experience",
        r"(\d+)\s*\+?\s*(?:years|yrs)\s+(?:of\s+)?experience",
        r"experience\s+(?:of\s+)?(\d+)\s*-\s*(\d+)\s*(?:years|yrs)",
        r"experience\s+(?:of\s+)?(\d+)\s*to\s*(\d+)\s*(?:years|yrs)",
        r"experience\s+(?:of\s+)?(\d+)\s*\+?\s*(?:years|yrs)",
    ]

    found_years: list[int] = []
    evidence = ""
    for pattern in patterns:
        for match in re.finditer(pattern, haystack):
            try:
                nums = [int(g) for g in match.groups() if g is not None]
                found_years.extend(nums)
                if not evidence:
                    evidence = match.group(0)
            except Exception:
                continue

    if not found_years:
        return {
            "detected_years": "",
            "experience_band": "unknown",
            "experience_evidence": "",
            "is_preferred": True,
            "skip_reason": "",
        }

    min_years = min(found_years)
    max_years = max(found_years)
    detected = f"{min_years}-{max_years}" if min_years != max_years else str(max_years)

    if max_years < cfg.min_preferred_years_experience:
        band = "below_preferred"
    elif min_years >= cfg.min_preferred_years_experience and max_years <= cfg.max_allowed_years_experience:
        band = "preferred"
    elif min_years <= cfg.max_allowed_years_experience < max_years:
        band = "mixed"
    else:
        band = "above_limit"

    skip_reason = ""
    if min_years > cfg.max_allowed_years_experience:
        skip_reason = f"requires_{detected}_years_experience"

    return {
        "detected_years": detected,
        "experience_band": band,
        "experience_evidence": evidence,
        "is_preferred": band in {"preferred", "mixed", "unknown"},
        "skip_reason": skip_reason,
    }


def click_easy_apply(page: Page) -> bool:
    candidates = [
        "button:has-text('Easy Apply')",
        "button[aria-label*='Easy Apply']",
        "a:has-text('Easy Apply')",
        "[role='button']:has-text('Easy Apply')",
    ]
    for sel in candidates:
        btn = page.locator(sel).first
        if btn.count() == 0:
            continue
        try:
            btn.click(timeout=8000)
            return True
        except Exception:
            continue
    return False


def field_has_prefilled_value(field: Locator) -> bool:
    try:
        return bool(field.input_value().strip())
    except Exception:
        try:
            val = field.get_attribute("value") or ""
            return bool(val.strip())
        except Exception:
            return False


def commit_text_field(dialog: Locator, field: Locator, label_text: str, typed_value: str) -> None:
    # LinkedIn typeahead inputs keep an open suggestion popover until a suggestion is selected.
    label_norm = normalize_question(label_text)
    typed_norm = normalize_question(typed_value)

    suggestion_selectors = [
        "[data-test-single-typeahead-entity-form-search-result='true']",
        ".search-typeahead-v2__hit",
        "li[role='option']",
    ]

    if any(token in label_norm for token in ["location", "city"]):
        for sel in suggestion_selectors:
            items = dialog.locator(sel)
            try:
                count = min(items.count(), 5)
            except Exception:
                count = 0
            for i in range(count):
                item = items.nth(i)
                text = normalize_question(safe_text(item))
                if typed_norm and typed_norm in text:
                    try:
                        item.click(timeout=1500)
                        time.sleep(0.3)
                        return
                    except Exception:
                        continue

    for sel in suggestion_selectors:
        item = dialog.locator(sel).first
        try:
            if item.count() > 0 and item.is_visible():
                item.click(timeout=1000)
                time.sleep(0.2)
                return
        except Exception:
            continue

    try:
        field.press("Enter")
        time.sleep(0.2)
        return
    except Exception:
        pass

    try:
        field.press("Tab")
        time.sleep(0.2)
    except Exception:
        pass


def get_field_label(dialog: Locator, field: Locator) -> str:
    field_id = field.get_attribute("id") or ""
    label_text = ""
    if field_id:
        label_text = safe_text(dialog.locator(f"label[for='{field_id}']").first)
    if not label_text:
        label_text = field.get_attribute("aria-label") or field.get_attribute("placeholder") or ""
    return label_text.strip()


def fill_text_like_field(
    dialog: Locator,
    field: Locator,
    cfg: BotConfig,
    memory: dict[str, Any],
    reason_tags: set[str],
) -> tuple[bool, str]:
    label_text = get_field_label(dialog, field)
    required = is_required(field)

    if field_has_prefilled_value(field):
        reason_tags.add("prefilled_linkedin")
        return True, ""

    if not required and not cfg.fill_optional_fields:
        return True, ""

    answer, source = resolve_answer(label_text, cfg, memory)
    if answer:
        field.fill(answer)
        commit_text_field(dialog, field, label_text, answer)
        if source:
            reason_tags.add(source)
        return True, ""

    if required:
        interactive = prompt_and_store_missing_answer("text", label_text, memory, cfg)
        if interactive is None:
            return False, f"Missing required text answer: {label_text or 'unknown'}"
        field.fill(interactive)
        commit_text_field(dialog, field, label_text, interactive)
        reason_tags.add("answered_interactively")

    return True, ""


def handle_select_field(
    dialog: Locator,
    select: Locator,
    cfg: BotConfig,
    memory: dict[str, Any],
    reason_tags: set[str],
) -> tuple[bool, str]:
    label_text = get_field_label(dialog, select)
    options = select.locator("option")
    required = is_required(select)

    try:
        current = select.input_value().strip()
        if current:
            reason_tags.add("prefilled_linkedin")
            return True, ""
    except Exception:
        pass

    if options.count() <= 1:
        return (not required, f"Missing options for required dropdown: {label_text or 'unknown'}")

    if not required and not cfg.fill_optional_fields:
        return True, ""

    answer, source = resolve_answer(label_text, cfg, memory)

    def try_select_by_answer(raw: str) -> bool:
        val = raw.strip().lower()
        for i in range(options.count()):
            opt = options.nth(i)
            opt_label = (opt.inner_text(timeout=1000) or "").strip().lower()
            opt_value = (opt.get_attribute("value") or "").strip().lower()
            if val and (val == opt_label or val == opt_value or val in opt_label):
                select.select_option(index=i)
                return True
        return False

    if answer and try_select_by_answer(answer):
        if source:
            reason_tags.add(source)
        return True, ""

    if required:
        interactive = prompt_and_store_missing_answer("dropdown", label_text, memory, cfg)
        if interactive is None:
            return False, f"Missing required dropdown answer: {label_text or 'unknown'}"
        if try_select_by_answer(interactive):
            reason_tags.add("answered_interactively")
            return True, ""
        return False, f"No dropdown option matched answer for: {label_text or 'unknown'}"

    return True, ""


def handle_radio_or_checkbox_group(
    group: Locator,
    cfg: BotConfig,
    memory: dict[str, Any],
    reason_tags: set[str],
) -> tuple[bool, str]:
    legend = safe_text(group.locator("legend").first)
    inputs = group.locator("input[type='radio'], input[type='checkbox']")
    if inputs.count() == 0:
        return True, ""
    required = "*" in legend or "required" in normalize_question(legend)

    for i in range(inputs.count()):
        inp = inputs.nth(i)
        if inp.is_checked():
            reason_tags.add("prefilled_linkedin")
            return True, ""

    if not required and not cfg.fill_optional_fields:
        return True, ""

    answer, source = resolve_answer(legend, cfg, memory)
    if answer:
        answer_norm = normalize_question(answer)
        for i in range(inputs.count()):
            inp = inputs.nth(i)
            opt_id = inp.get_attribute("id")
            label = ""
            if opt_id:
                label = safe_text(group.locator(f"label[for='{opt_id}']").first)
            label_norm = normalize_question(label)
            if answer_norm and (answer_norm == label_norm or answer_norm in label_norm):
                inp.check(force=True)
                if source:
                    reason_tags.add(source)
                return True, ""

    if not required:
        return True, ""

    interactive = prompt_and_store_missing_answer("choice", legend, memory, cfg)
    if interactive is None:
        return False, f"Missing required choice answer: {legend or 'unknown'}"

    interactive_norm = normalize_question(interactive)
    for i in range(inputs.count()):
        inp = inputs.nth(i)
        opt_id = inp.get_attribute("id")
        label = ""
        if opt_id:
            label = safe_text(group.locator(f"label[for='{opt_id}']").first)
        label_norm = normalize_question(label)
        if interactive_norm and (interactive_norm == label_norm or interactive_norm in label_norm):
            inp.check(force=True)
            reason_tags.add("answered_interactively")
            return True, ""

    return False, f"No option matched answer for required choice: {legend or 'unknown'}"


def resolve_resume_path(cfg: BotConfig, memory: dict[str, Any]) -> Path | None:
    candidates = [cfg.profile.resume_pdf_path, str(memory.get("resume_pdf_path", ""))]
    for c in candidates:
        if not c:
            continue
        p = Path(c).expanduser().resolve()
        if p.exists() and p.is_file() and p.suffix.lower() == ".pdf":
            return p

    if cfg.interactive_missing_data:
        entered = input("Resume PDF required. Enter absolute PDF path (blank to skip this job): ").strip()
        if entered:
            p = Path(entered).expanduser().resolve()
            if p.exists() and p.is_file() and p.suffix.lower() == ".pdf":
                cfg.profile.resume_pdf_path = str(p)
                memory["resume_pdf_path"] = str(p)
                return p
            print("Invalid path or not a PDF. Job will be skipped.")

    return None


def upload_resume_if_needed(
    dialog: Locator,
    cfg: BotConfig,
    memory: dict[str, Any],
    reason_tags: set[str],
) -> tuple[bool, str]:
    file_inputs = dialog.locator("input[type='file']")
    if file_inputs.count() == 0:
        return True, ""

    for i in range(file_inputs.count()):
        inp = file_inputs.nth(i)
        if not inp.is_visible():
            continue
        required = is_required(inp)
        if not required and not cfg.fill_optional_fields:
            continue

        resume = resolve_resume_path(cfg, memory)
        if resume is None:
            return False, "Resume required but unavailable"
        try:
            inp.set_input_files(str(resume))
            reason_tags.add("resume_uploaded")
            return True, ""
        except Exception:
            continue

    return True, ""


def answer_current_step(
    page: Page,
    cfg: BotConfig,
    memory: dict[str, Any],
    reason_tags: set[str],
) -> tuple[bool, str]:
    dialog = page.locator("div[role='dialog']").last
    if dialog.count() == 0:
        return False, "Application dialog missing"

    ok, reason = upload_resume_if_needed(dialog, cfg, memory, reason_tags)
    if not ok:
        return False, reason

    text_fields = dialog.locator(
        "input:not([type='hidden']):not([type='radio']):not([type='checkbox']):not([type='file']), textarea"
    )
    for i in range(text_fields.count()):
        field = text_fields.nth(i)
        if not field.is_visible():
            continue
        ok, reason = fill_text_like_field(dialog, field, cfg, memory, reason_tags)
        if not ok:
            return False, reason

    selects = dialog.locator("select")
    for i in range(selects.count()):
        select = selects.nth(i)
        if not select.is_visible():
            continue
        ok, reason = handle_select_field(dialog, select, cfg, memory, reason_tags)
        if not ok:
            return False, reason

    groups = dialog.locator("fieldset")
    for i in range(groups.count()):
        group = groups.nth(i)
        if not group.is_visible():
            continue
        ok, reason = handle_radio_or_checkbox_group(group, cfg, memory, reason_tags)
        if not ok:
            return False, reason

    return True, ""


def clear_typeahead_overlay(page: Page) -> None:
    candidates = [
        "[data-test-single-typeahead-entity-form-search-result='true']",
        ".search-typeahead-v2__hit",
        "li[role='option']",
    ]
    for sel in candidates:
        try:
            items = page.locator(sel)
            if items.count() == 0:
                continue
            first = items.first
            if first.is_visible():
                first.click(timeout=1000)
                time.sleep(0.2)
                return
        except Exception:
            continue

    try:
        page.keyboard.press("Escape")
        time.sleep(0.1)
    except Exception:
        pass


def clear_discard_confirmation_overlay(page: Page) -> None:
    # LinkedIn sometimes shows discard-confirmation as top-layer modal and blocks clicks.
    candidates = [
        "button:has-text('Continue applying')",
        "button:has-text('Keep applying')",
        "button:has-text('Cancel')",
    ]
    for sel in candidates:
        btn = page.locator(sel).first
        if btn.count() == 0:
            continue
        try:
            if btn.is_visible():
                btn.click(timeout=1500)
                time.sleep(0.3)
                return
        except Exception:
            continue


def stabilize_easy_apply_modal(page: Page) -> None:
    # Resolve transient overlays before interacting with the step buttons.
    clear_typeahead_overlay(page)
    clear_discard_confirmation_overlay(page)


def locate_step_button(page: Page, step: str) -> Locator:
    selectors = {
        "submit": [
            "div[role='dialog'] button[data-live-test-easy-apply-submit-button]",
            "div[role='dialog'] button[aria-label='Submit application']",
            "div[role='dialog'] button:has-text('Submit application')",
        ],
        "review": [
            "div[role='dialog'] button[aria-label='Review your application']",
            "div[role='dialog'] button:has-text('Review')",
        ],
        "next": [
            "div[role='dialog'] button[data-easy-apply-next-button]",
            "div[role='dialog'] button[data-live-test-easy-apply-next-button]",
            "div[role='dialog'] button[aria-label='Continue to next step']",
            "div[role='dialog'] button:has-text('Next')",
            "div[role='dialog'] button:has-text('Continue')",
        ],
    }
    for sel in selectors[step]:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return page.locator("__codex_no_match__").first


def click_step_button(page: Page, step: str) -> bool:
    stabilize_easy_apply_modal(page)
    btn = locate_step_button(page, step)
    try:
        if btn.count() == 0 or not btn.is_visible():
            return False
    except Exception:
        return False

    for _ in range(2):
        try:
            btn.click(timeout=6000)
            return True
        except Exception:
            stabilize_easy_apply_modal(page)
            try:
                btn.click(timeout=3000, force=True)
                return True
            except Exception:
                continue
    return False


def save_or_discard_application(page: Page) -> str:
    try:
        save_btn = page.locator("button:has-text('Save')").first
        if save_btn.count() > 0 and save_btn.is_visible():
            save_btn.click(timeout=3000)
            time.sleep(0.5)
            return "saved_for_retry"
    except Exception:
        pass

    try:
        if click_step_button(page, "Dismiss"):
            time.sleep(0.8)
            save_btn = page.locator("button:has-text('Save')").first
            if save_btn.count() > 0 and save_btn.is_visible():
                save_btn.click(timeout=3000)
                time.sleep(0.5)
                return "saved_for_retry"
            page.locator("button:has-text('Discard')").first.click(timeout=3000)
            return "discarded"
    except Exception:
        pass

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    return "discarded"


def verify_application_submitted(page: Page, job_url: str) -> bool:
    selectors = [
        "text=Application submitted",
        "button:has-text('Applied')",
        "button:has-text('Application submitted')",
        "button[aria-label*='Applied']",
    ]

    def has_confirmation() -> bool:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    for _ in range(8):
        if has_confirmation():
            return True
        time.sleep(1)

    try:
        page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
    except Exception:
        return False

    return has_confirmation()
    


def complete_easy_apply(page: Page, cfg: BotConfig, memory: dict[str, Any], job_url: str) -> tuple[bool, str]:
    max_steps = 12
    reason_tags: set[str] = set()

    for _ in range(max_steps):
        ok, reason = answer_current_step(page, cfg, memory, reason_tags)
        if not ok:
            exit_reason = save_or_discard_application(page)
            reason_tags.add(exit_reason)
            return False, ";".join(sorted([reason] + list(reason_tags)))

        human_delay(cfg)

        if click_step_button(page, "next"):
            human_delay(cfg)
            continue

        if click_step_button(page, "review"):
            human_delay(cfg)
            if cfg.auto_submit and click_step_button(page, "submit"):
                human_delay(cfg)
                if verify_application_submitted(page, job_url):
                    reason_tags.add("applied")
                    return True, ";".join(sorted(reason_tags))
                exit_reason = save_or_discard_application(page)
                reason_tags.add("submission_unconfirmed")
                reason_tags.add(exit_reason)
                return False, ";".join(sorted(reason_tags))
            if not cfg.auto_submit:
                reason_tags.add("review_reached")
                return False, ";".join(sorted(reason_tags))

        if click_step_button(page, "submit"):
            human_delay(cfg)
            if verify_application_submitted(page, job_url):
                reason_tags.add("applied")
                return True, ";".join(sorted(reason_tags))
            exit_reason = save_or_discard_application(page)
            reason_tags.add("submission_unconfirmed")
            reason_tags.add(exit_reason)
            return False, ";".join(sorted(reason_tags))

        exit_reason = save_or_discard_application(page)
        reason_tags.add("unsupported_step")
        reason_tags.add(exit_reason)
        return False, ";".join(sorted(reason_tags))

    exit_reason = save_or_discard_application(page)
    reason_tags.add("max_steps_exceeded")
    reason_tags.add(exit_reason)
    return False, ";".join(sorted(reason_tags))


def process_single_job(
    page: Page,
    job_url: str,
    idx: int,
    cfg: BotConfig,
    applied_ids: set[str],
    memory: dict[str, Any],
    search_title: str,
) -> dict[str, str]:
    page.goto(job_url, wait_until="domcontentloaded")
    human_delay(cfg)

    job_id = extract_job_id_from_url(page.url)
    title, company = extract_job_meta(page, idx)

    if not job_id:
        job_id = f"{title}|{company}"

    if job_id in applied_ids:
        return {
            "search_title": search_title,
            "job_id": job_id,
            "job_title": title,
            "company": company,
            "status": "skipped",
            "reason": "already_applied",
            "experience_detected_years": "",
            "experience_band": "unknown",
            "experience_evidence": "",
        }

    if company_is_excluded(company, cfg):
        return {
            "search_title": search_title,
            "job_id": job_id,
            "job_title": title,
            "company": company,
            "status": "skipped",
            "reason": "excluded_company",
            "experience_detected_years": "",
            "experience_band": "unknown",
            "experience_evidence": "",
        }

    exp_info = detect_experience_requirement(page, cfg)
    if exp_info["skip_reason"]:
        return {
            "search_title": search_title,
            "job_id": job_id,
            "job_title": title,
            "company": company,
            "status": "skipped",
            "reason": exp_info["skip_reason"],
            "experience_detected_years": exp_info["detected_years"],
            "experience_band": exp_info["experience_band"],
            "experience_evidence": exp_info["experience_evidence"],
        }

    if not click_easy_apply(page):
        return {
            "search_title": search_title,
            "job_id": job_id,
            "job_title": title,
            "company": company,
            "status": "skipped",
            "reason": "easy_apply_button_not_found",
            "experience_detected_years": exp_info["detected_years"],
            "experience_band": exp_info["experience_band"],
            "experience_evidence": exp_info["experience_evidence"],
        }

    human_delay(cfg)
    ok, reason = complete_easy_apply(page, cfg, memory, job_url)
    status = "applied" if ok else "skipped"

    if ok:
        applied_ids.add(job_id)

    return {
        "search_title": search_title,
        "job_id": job_id,
        "job_title": title,
        "company": company,
        "status": status,
        "reason": reason,
        "experience_detected_years": exp_info["detected_years"],
        "experience_band": exp_info["experience_band"],
        "experience_evidence": exp_info["experience_evidence"],
    }


# =========================
# Main
# =========================
def main() -> None:
    cfg = CONFIG
    results_csv = Path(cfg.results_csv)
    applied_file = Path(cfg.applied_jobs_file)
    memory_file = Path(cfg.answer_memory_file)

    ensure_results_csv(results_csv)
    applied_ids = load_applied_jobs(applied_file)
    memory = memory_load(memory_file)

    context, page = launch_persistent_browser(cfg)
    logging.info("Browser launched with persistent profile at: %s", cfg.user_data_dir)

    try:
        applied_count = 0
        wait_for_manual_login(page)
        search_titles = cfg.search_job_titles or [cfg.job_title]
        for search_title in search_titles:
            cfg.job_title = search_title
            run_job_search(page, cfg)
            apply_easy_apply_filter(page, cfg)

            job_links = collect_job_links(page, cfg.scan_links_limit)
            if not job_links:
                logging.warning("No job links found for search title: %s", search_title)
                continue

            logging.info(
                "Scanning up to %s job link(s) for '%s' to complete %s application(s)...",
                len(job_links),
                search_title,
                cfg.max_jobs - applied_count,
            )

            for idx, job_url in enumerate(job_links):
                try:
                    result = process_single_job(page, job_url, idx, cfg, applied_ids, memory, search_title)
                except TimeoutError as e:
                    result = {
                        "search_title": search_title,
                        "job_id": f"unknown_{idx + 1}",
                        "job_title": f"Unknown Job #{idx + 1}",
                        "company": "Unknown Company",
                        "status": "error",
                        "reason": f"timeout:{e}",
                        "experience_detected_years": "",
                        "experience_band": "unknown",
                        "experience_evidence": "",
                    }
                except Exception as e:
                    result = {
                        "search_title": search_title,
                        "job_id": f"unknown_{idx + 1}",
                        "job_title": f"Unknown Job #{idx + 1}",
                        "company": "Unknown Company",
                        "status": "error",
                        "reason": f"error:{e}",
                        "experience_detected_years": "",
                        "experience_band": "unknown",
                        "experience_evidence": "",
                    }

                row = {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    **result,
                }
                append_result(results_csv, row)
                logging.info(
                    "[%s/%s] %s | %s | %s | %s | exp=%s | band=%s",
                    idx + 1,
                    len(job_links),
                    result["job_title"],
                    result["company"],
                    result["status"],
                    result["reason"],
                    result["experience_detected_years"] or "unknown",
                    result["experience_band"],
                )
                human_delay(cfg)
                if result["status"] == "applied":
                    applied_count += 1
                if applied_count >= cfg.max_jobs:
                    logging.info("Reached max successful applications for this run: %s", cfg.max_jobs)
                    break

            if applied_count >= cfg.max_jobs:
                break

    finally:
        save_applied_jobs(applied_file, applied_ids)
        memory_save(memory_file, memory)
        close_persistent_browser(context)
        logging.info("Done. Results saved to %s", results_csv)
        logging.info("Memory saved to %s", memory_file)


if __name__ == "__main__":
    main()
