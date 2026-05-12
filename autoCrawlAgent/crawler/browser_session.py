from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

MAX_PAGE_TEXT = 16000
MAX_INTERACTIVE = 72
# 每种按钮选择器最多采集条数（避免超大页卡顿）；过小会导致顶栏按钮被漏掉
MAX_BUTTONS_PER_SELECTOR = 22

# 导航区链接：用于优先展示与点击（栏目、站点菜单等）；:visible 避免与隐藏离屏链 nth 错位
NAV_LINK_COMPOUND = (
    'nav a[href]:visible, header a[href]:visible, [role="navigation"] a[href]:visible'
)
# 下拉 / listbox / Radix 浮层。含 [role="menu"] 但排除 mmenu 离屏抽屉（见 collect 内过滤）
MENU_LINK_COMPOUND = (
    '[role="menu"] a[href]:visible, '
    '[role="menubar"] [role="menu"] a[href]:visible, '
    '[role="listbox"] a[href]:visible, '
    '[data-radix-popper-content-wrapper] a[href]:visible'
)


def _css_attr_id(id_val: str) -> str:
    """[id="..."] 选择器，转义引号与反斜杠。"""
    s = id_val.replace("\\", "\\\\").replace('"', '\\"')
    return f'[id="{s}"]'


def _panel_anchors_compound(panel_id: str) -> str:
    return f'{_css_attr_id(panel_id)} a[href]:visible'


def _norm_url_for_cmp(u: str) -> str:
    """与 graph._norm_url 一致：忽略 fragment，规范化 scheme/host/path，用于判断当前页是否已导航。"""
    u = (u or "").strip()
    if not u:
        return ""
    try:
        if "://" not in u and u.startswith("//"):
            u = "https:" + u
        elif "://" not in u:
            u = "https://" + u.lstrip("/")
        p = urlparse(u)
        scheme = (p.scheme or "https").lower()
        if scheme not in ("http", "https"):
            return ""
        netloc = (p.netloc or "").lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = p.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        path = path or "/"
        return f"{scheme}://{netloc}{path}".lower()
    except Exception:
        return (u or "").split("#")[0].lower().rstrip("/")


def _host_for_cmp(url: str) -> str:
    try:
        p = urlparse(url)
        h = (p.netloc or "").lower()
        if h.startswith("www."):
            h = h[4:]
        return h
    except Exception:
        return ""


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    return s[: n - 20] + "\n...[截断]..."


def _norm_key(text: str, href: str) -> Tuple[str, str]:
    return (text.strip()[:200], (href or "").strip())


def _locator_button_label(el: Any) -> str:
    """可见按钮的展示文案：优先正文，其次 aria-label / title 等（图标按钮常无 inner_text）。"""
    try:
        raw = (el.inner_text() or "").strip()
        if raw:
            return _truncate(raw, 120)
    except Exception:
        pass
    for attr in ("value", "aria-label", "title", "placeholder", "alt"):
        try:
            v = (el.get_attribute(attr) or "").strip()
            if v:
                return _truncate(v, 120)
        except Exception:
            continue
    try:
        lab = el.evaluate(
            """e => {
              const id = e.getAttribute('aria-labelledby');
              if (!id) return '';
              return id.trim().split(/\\s+/).map(x => {
                const n = document.getElementById(x);
                return n ? (n.innerText || n.textContent || '').trim() : '';
              }).filter(Boolean).join(' ');
            }"""
        )
        if isinstance(lab, str) and lab.strip():
            return _truncate(lab.strip(), 120)
    except Exception:
        pass
    try:
        hint = el.evaluate(
            """e => {
              const tag = e.tagName.toLowerCase();
              const id = e.id ? '#' + e.id : '';
              const dt = e.getAttribute('data-testid') || e.getAttribute('name') || '';
              return tag + id + (dt ? ' ' + dt : '');
            }"""
        )
        if isinstance(hint, str) and hint.strip():
            return _truncate(f"[按钮] {hint.strip()}", 120)
    except Exception:
        pass
    return "[无文案按钮]"


@dataclass
class BrowserSession:
    """同步 Playwright 会话：打开页面、读取正文、枚举并点击可交互元素。"""

    headless: bool = True
    _pw: Optional[Playwright] = None
    _browser: Optional[Browser] = None
    page: Optional[Page] = None
    current_url: str = ""
    last_interaction: str = ""

    def _log_open(self, reason: str, url: str, extra: str = "") -> None:
        """在 stderr 打印页面打开/导航，便于监控多页流程（不污染 stdout JSON）。"""
        line = f"[AgentCrawler] 打开页面 | {reason} | {url}"
        if extra:
            line += f" | {extra}"
        print(line, file=sys.stderr)

    def start(self) -> None:
        if self._browser:
            return
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        # 桌面视口：多数高校站 mega menu 在窄屏下收进汉堡，展开层内链接不会被采集到
        self.page = self._browser.new_page(viewport={"width": 1360, "height": 900})
        self.page.set_default_timeout(45_000)

    def stop(self) -> None:
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._pw:
            self._pw.stop()
            self._pw = None
        self.page = None

    def goto(self, url: str) -> None:
        self.start()
        assert self.page is not None
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self.current_url = self.page.url
        extra = ""
        if self.current_url.rstrip("/") != url.rstrip("/"):
            extra = f"请求URL={url}"
        self._log_open("goto", self.current_url, extra)

    def refresh_content(self) -> None:
        assert self.page is not None
        try:
            self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

    def expand_collapsed_content(
        self,
        *,
        max_rounds: int = 6,
        max_clicks_per_round: int = 22,
    ) -> None:
        """
        快照前尽量展开折叠内容，使 inner_text / :visible 能采到手风琴、details 等内部正文。
        1) 将所有 <details> 设为 open；2) 在主内容区轮询点击 aria-expanded=false 的按钮。
        """
        assert self.page is not None
        # 常见手风琴触发器：限定在正文区域，避免乱点顶栏汉堡/全局菜单
        expand_trigger_sel = (
            'main button[aria-expanded="false"]:visible, '
            '[role="main"] button[aria-expanded="false"]:visible, '
            '#main-content button[aria-expanded="false"]:visible, '
            '#content button[aria-expanded="false"]:visible, '
            '#main button[aria-expanded="false"]:visible, '
            'article button[aria-expanded="false"]:visible, '
            'main [role="button"][aria-expanded="false"]:visible, '
            '[role="main"] [role="button"][aria-expanded="false"]:visible, '
            '#primary button[aria-expanded="false"]:visible, '
            '.entry-content button[aria-expanded="false"]:visible'
        )
        total_details = 0
        total_btn = 0
        for _ in range(max(1, max_rounds)):
            try:
                n_det = self.page.evaluate(
                    """() => {
                        let n = 0;
                        document.querySelectorAll('details:not([open])').forEach(el => {
                            el.open = true;
                            n++;
                        });
                        return n;
                    }"""
                )
                if isinstance(n_det, int) and n_det > 0:
                    total_details += n_det
            except Exception:
                pass

            loc = self.page.locator(expand_trigger_sel)
            round_clicks = 0
            for _ in range(max_clicks_per_round):
                try:
                    if loc.count() == 0:
                        break
                    first = loc.first
                    if not first.is_visible():
                        break
                    first.click(timeout=2_500)
                    round_clicks += 1
                    total_btn += 1
                    try:
                        self.page.wait_for_timeout(100)
                    except Exception:
                        pass
                except Exception:
                    break
            if round_clicks == 0:
                break

        if total_details or total_btn:
            print(
                f"[AgentCrawler] 展开折叠: details≈{total_details} 次, 按钮≈{total_btn} 次",
                file=sys.stderr,
            )
        try:
            self.page.wait_for_timeout(150)
        except Exception:
            pass

    def visible_text(self) -> str:
        assert self.page is not None
        try:
            body = self.page.locator("body").inner_text(timeout=10_000)
        except Exception:
            body = self.page.content()
        return _truncate(body, MAX_PAGE_TEXT)

    def collect_interactives(self) -> List[Dict[str, Any]]:
        assert self.page is not None
        base = self.page.url
        items: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, str]] = set()

        # 1) 导航区链接优先（栏目/菜单，进入子页继续整合信息）
        nav_loc = self.page.locator(NAV_LINK_COMPOUND)
        n_nav = min(nav_loc.count(), 52)
        for i in range(n_nav):
            a = nav_loc.nth(i)
            try:
                if not a.is_visible():
                    continue
                text = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                if not text and not href:
                    continue
                abs_url = urljoin(base, href) if href else ""
                key = _norm_key(text, abs_url)
                if key in seen:
                    continue
                seen.add(key)
                items.append(
                    {
                        "role": "link",
                        "text": _truncate(text, 120),
                        "href": abs_url,
                        "is_nav": True,
                        "_click": ("scoped", NAV_LINK_COMPOUND, i),
                    }
                )
            except Exception:
                continue

        # 1b) 顶栏已展开 mega / 下拉：aria-controls 指向的面板内链接（future.utoronto 等）
        exp = self.page.locator(
            'header [aria-expanded="true"], [role="navigation"] [aria-expanded="true"]'
        )
        panel_ids: List[str] = []
        for ei in range(min(exp.count(), 24)):
            btn = exp.nth(ei)
            try:
                ac = (btn.get_attribute("aria-controls") or "").strip()
                for part in ac.split():
                    pid = part.strip()
                    if pid and pid not in panel_ids:
                        panel_ids.append(pid)
            except Exception:
                continue
        for pid in panel_ids[:14]:
            sel = _panel_anchors_compound(pid)
            try:
                pan = self.page.locator(_css_attr_id(pid))
                if pan.count() == 0:
                    continue
            except Exception:
                continue
            al = self.page.locator(sel)
            n_a = min(al.count(), 40)
            for pi in range(n_a):
                a = al.nth(pi)
                try:
                    if not a.is_visible():
                        continue
                    text = (a.inner_text() or "").strip()
                    href = a.get_attribute("href") or ""
                    if not text and not href:
                        continue
                    abs_url = urljoin(base, href) if href else ""
                    key = _norm_key(text, abs_url)
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(
                        {
                            "role": "link",
                            "text": _truncate(text, 120),
                            "href": abs_url,
                            "is_nav": False,
                            "is_menu": True,
                            "_click": ("menulink", sel, pi),
                        }
                    )
                except Exception:
                    continue

        # 2) 展开后的菜单 / 下拉 / Radix 浮层内链接（排在全站 a 之前，避免被截断）
        menu_loc = self.page.locator(MENU_LINK_COMPOUND)
        n_menu = min(menu_loc.count(), 50)
        for mi in range(n_menu):
            a = menu_loc.nth(mi)
            try:
                if not a.is_visible():
                    continue
                if a.evaluate(
                    """el => !!el.closest(
                        '#menu-mobile,.m-menu-mobile,.mm-menu_offcanvas,.mm-wrapper,.mm-menu'
                    )"""
                ):
                    continue
                text = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                if not text and not href:
                    continue
                abs_url = urljoin(base, href) if href else ""
                key = _norm_key(text, abs_url)
                if key in seen:
                    continue
                seen.add(key)
                items.append(
                    {
                        "role": "link",
                        "text": _truncate(text, 120),
                        "href": abs_url,
                        "is_nav": False,
                        "is_menu": True,
                        "_click": ("menulink", MENU_LINK_COMPOUND, mi),
                    }
                )
            except Exception:
                continue

        # 3) 按钮类（放在全站 a[href] 之前，避免 MAX_INTERACTIVE 截断时只剩正文链接、没有顶栏按钮）
        button_selectors = (
            'button:visible',
            '[role="button"]:visible',
            'input[type="submit"]:visible',
            'input[type="button"]:visible',
            'input[type="reset"]:visible',
            'input[type="image"]:visible',
            'summary:visible',
        )
        seen_btn: Set[Tuple[str, int]] = set()
        for sel in button_selectors:
            loc = self.page.locator(sel)
            n = min(loc.count(), MAX_BUTTONS_PER_SELECTOR)
            for i in range(n):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    text = _locator_button_label(el)
                    bid = (sel, i)
                    if bid in seen_btn:
                        continue
                    seen_btn.add(bid)
                    items.append(
                        {
                            "role": "button",
                            "text": text,
                            "is_nav": False,
                            "_click": ("btn", sel, i),
                        }
                    )
                except Exception:
                    continue

        # 4) 其余页面链接（:visible 使 nth 与「可见链接顺序」一致，避免前 35 个 DOM 位全是隐藏链导致漏掉主区 CTA）
        link_loc = self.page.locator("a[href]:visible")
        n_links = min(link_loc.count(), 56)
        for j in range(n_links):
            a = link_loc.nth(j)
            try:
                if not a.is_visible():
                    continue
                text = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                abs_url = urljoin(base, href) if href else ""
                key = _norm_key(text, abs_url)
                if key in seen:
                    continue
                seen.add(key)
                items.append(
                    {
                        "role": "link",
                        "text": _truncate(text, 120),
                        "href": abs_url,
                        "is_nav": False,
                        "_click": ("link", j),
                    }
                )
            except Exception:
                continue

        return items[:MAX_INTERACTIVE]

    def interactives_as_text(self, items: List[Dict[str, Any]]) -> str:
        lines = []
        for i, it in enumerate(items):
            role = it.get("role", "")
            text = it.get("text", "")
            href = it.get("href", "")
            marks: List[str] = []
            if it.get("is_menu"):
                marks.append("菜单/下拉内")
            if it.get("is_nav"):
                marks.append("导航链接")
            nav_mark = f" [{'|'.join(marks)}]" if marks else ""
            extra = f" href={href}" if href else ""
            lines.append(f"[{i}] {role}{nav_mark}: {text}{extra}")
        if not lines:
            return "(无可交互元素)"
        return "\n".join(lines)

    def _link_click_follow(
        self,
        loc: Any,
        href: str,
        url_start: str,
        *,
        link_text: str,
        verb: str = "点击链接",
        on_click_fail_goto: bool = True,
    ) -> None:
        """
        点击带 href 的链接并跟随导航：处理 target=_blank 新开标签、点击失败 goto、
        以及点击成功但当前页 URL 未变时的跨域 goto（避免误判「无页面跳转」）。
        """
        assert self.page is not None
        ctx = self.page.context
        raw_href = (href or "").strip()
        abs_href = urljoin(url_start, raw_href) if raw_href else ""
        if raw_href.lower().startswith("javascript:") or raw_href.startswith("#"):
            abs_href = ""

        n_before = len(ctx.pages)
        try:
            loc.click(timeout=10_000)
        except Exception:
            if on_click_fail_goto and abs_href:
                self._log_open("click_fallback_goto", abs_href, "链接点击失败")
                self.page.goto(abs_href, wait_until="domcontentloaded")
                self.last_interaction = f"导航打开: {abs_href[:120]}"
                return
            raise

        try:
            self.page.wait_for_timeout(150)
        except Exception:
            pass

        if len(ctx.pages) > n_before:
            new_p = ctx.pages[-1]
            try:
                new_p.wait_for_load_state("domcontentloaded", timeout=45_000)
            except Exception:
                pass
            old = self.page
            self.page = new_p
            self.page.set_default_timeout(45_000)
            self.last_interaction = (
                f"{verb}（新标签）: {(link_text or '')[:80]} -> {new_p.url[:120]}"
            )
            try:
                old.close()
            except Exception:
                pass
            return

        if _norm_url_for_cmp(self.page.url) != _norm_url_for_cmp(url_start):
            self.last_interaction = f"{verb}: {(link_text or '')[:80]}"
            return

        if abs_href:
            h_link = _host_for_cmp(abs_href)
            h_here = _host_for_cmp(url_start)
            if h_link and h_here and h_link != h_here:
                self._log_open(
                    "goto",
                    abs_href,
                    "当前页 URL 未变（可能弹窗被拦或未触发同页导航），外链在当前标签打开",
                )
                self.page.goto(abs_href, wait_until="domcontentloaded")
                self.last_interaction = f"{verb}（外链 goto）: {(link_text or '')[:80]}"
                return

        self.last_interaction = f"{verb}: {(link_text or '')[:80]}"

    def click_index(self, items: List[Dict[str, Any]], index: int) -> str:
        assert self.page is not None
        if index < 0 or index >= len(items):
            raise IndexError("交互序号越界")
        it = items[index]
        ck = it.get("_click")
        if not ck:
            raise RuntimeError("缺少点击元数据")

        url_start = self.page.url
        kind = ck[0]
        text_short = (it.get("text") or "")[:80]
        if kind == "scoped":
            _, compound_sel, i = ck
            loc = self.page.locator(compound_sel).nth(int(i))
            href = (it.get("href") or "").strip()
            if href:
                self._link_click_follow(
                    loc,
                    href,
                    url_start,
                    link_text=text_short,
                    verb="点击导航链接",
                    on_click_fail_goto=True,
                )
            else:
                loc.click(timeout=10_000)
                self.last_interaction = f"点击导航链接: {text_short}"
        elif kind == "link":
            _, j = ck
            loc = self.page.locator("a[href]:visible").nth(int(j))
            href = (it.get("href") or "").strip()
            if href and urlparse(href).netloc == urlparse(self.page.url).netloc:
                self._link_click_follow(
                    loc,
                    href,
                    url_start,
                    link_text=text_short,
                    verb="点击链接",
                    on_click_fail_goto=True,
                )
            else:
                if href:
                    self._log_open("goto", href, "外链或跨域")
                    self.page.goto(href, wait_until="domcontentloaded")
                else:
                    loc.click(timeout=10_000)
                self.last_interaction = f"打开链接: {href or it.get('text', '')}"
        elif kind == "btn":
            _, sel, i = ck
            self.page.locator(sel).nth(int(i)).click(timeout=10_000)
            self.last_interaction = f"点击控件: {text_short}"
        elif kind == "menulink":
            _, sel, i = ck
            loc = self.page.locator(sel).nth(int(i))
            href = (it.get("href") or "").strip()
            if href:
                self._link_click_follow(
                    loc,
                    href,
                    url_start,
                    link_text=text_short,
                    verb="点击菜单项",
                    on_click_fail_goto=True,
                )
            else:
                loc.click(timeout=10_000)
                self.last_interaction = f"点击菜单项: {text_short}"
        else:
            raise RuntimeError(f"未知点击类型: {kind}")

        self.current_url = self.page.url
        # 同 URL 时不要用 networkidle：易导致焦点丢失、下拉/巨型菜单被收起
        try:
            nu = urlparse(self.current_url)
            ns = urlparse(url_start)
            same_page = (nu.netloc == ns.netloc) and ((nu.path or "/").rstrip("/") == (ns.path or "/").rstrip("/"))
        except Exception:
            same_page = self.current_url == url_start
        if same_page:
            try:
                self.page.wait_for_timeout(400)
            except Exception:
                pass
        else:
            self.refresh_content()
        self._log_open("交互后", self.current_url, self.last_interaction[:120])
        return self.last_interaction
