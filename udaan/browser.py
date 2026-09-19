"""Udaan Browser: the sole browser vendor boundary; no challenge-solving APIs."""
import asyncio
import ipaddress
import re
import socket
from datetime import timedelta
from uuid import uuid4
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from udaan.config import RESILIENCE, settings
from udaan.contracts import DomainError, Inspection, PageState
from udaan.db import now
from udaan.health import browser_health


def ensure_browser_branding():
    """Patch only product-name resources; preserve engine files and license notices.

    Re-applied after upstream browser installs. A lock and atomic replacement keep
    simultaneous launches from reading a partial archive. Original bytes are retained.
    """
    import fcntl
    import os
    import shutil
    import tempfile
    import zipfile
    from pathlib import Path

    from camoufox.pkgman import get_path

    archive = Path(get_path("camoufox-bin")).parent / "browser" / "omni.ja"
    entries = {
        "localization/en-US/branding/brand.ftl",
        "chrome/en-US/locale/branding/brand.dtd",
        "chrome/en-US/locale/branding/brand.properties",
    }
    with archive.with_suffix(".udaan.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        from udaan.branding import browser_icons
        browser_icons()
        with zipfile.ZipFile(archive) as original:
            if not entries.issubset(original.namelist()):
                raise DomainError("BROWSER_BRANDING_UNSUPPORTED", "Installed browser branding resources are not supported; reinstall the supported browser version.")
            if all(b"Camoufox" not in original.read(name) for name in entries):
                return
            backup = archive.with_suffix(".upstream.ja")
            shutil.copy2(archive, backup)
            fd, temporary = tempfile.mkstemp(prefix="udaan-brand-", dir=archive.parent)
            os.close(fd)
            try:
                with zipfile.ZipFile(temporary, "w") as branded:
                    for info in original.infolist():
                        data = original.read(info.filename)
                        if info.filename in entries:
                            data = data.replace(b"Camoufox", b"Udaan Browser")
                        branded.writestr(info, data)
                os.chmod(temporary, archive.stat().st_mode)
                os.replace(temporary, archive)
            finally:
                Path(temporary).unlink(missing_ok=True)


class UdaanBrowser:
    def __init__(self, allowed_hosts: list[str], profile="Standard", *, slots=None, egress=None):
        from udaan.global_ip import GlobalIPDataset
        self.slots = slots
        self.egress = egress or GlobalIPDataset().get("automatic")
        self.context_id = None
        self.barrier_seen = False
        self.allowed_hosts = set(allowed_hosts)
        self.profile = RESILIENCE[profile]
        self.page = self.context = self.browser = None
        self._planner_request = None
        self.manager = None
        self.response_status = None
        self.retry_after = None
        self._public_hosts = {}

    @property
    def alive(self):
        return self.browser is not None and self.browser.is_connected() and self.page is not None and not self.page.is_closed()

    async def __aenter__(self):
        if self.slots is not None:
            try:
                self.browser = await self.slots.acquire(self)
                await self.new_page_context()
                return self
            except Exception:
                try:
                    if self.context:
                        await self.context.close()
                finally:
                    await self.slots.release(self)
                raise
        health = await browser_health()
        if health["status"] != "READY":
            raise DomainError("DISPLAY_UNAVAILABLE" if not settings().display else "BROWSER_UNAVAILABLE", health["reason"])
        await asyncio.to_thread(ensure_browser_branding)
        from camoufox.async_api import AsyncCamoufox
        self.manager = AsyncCamoufox(headless=False, env={"DISPLAY": settings().display},
                                    humanize=False, block_images=False, disable_coop=False,
                                    firefox_user_prefs={"javascript.enabled": True})
        try:
            self.browser = await self.manager.__aenter__()
            await self.new_page_context()
            return self
        except Exception as exc:
            if self.manager:
                await self.manager.__aexit__(None, None, None)
            raise DomainError("BROWSER_UNAVAILABLE", "Udaan Browser failed to launch") from exc

    async def new_page_context(self):
        options = {'viewport': {'width': 1280, 'height': 780}}
        proxy = self.egress.browser_proxy()
        if proxy:
            options['proxy'] = proxy
        self.context = await self.browser.new_context(**options)
        self.context_id = str(uuid4())
        await self.context.route('**/*', self._route)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(self.profile.element_ms)
        self.page.set_default_navigation_timeout(self.profile.navigation_ms)
        self.page.on('response', self._response)

    async def recover_page(self):
        if self.barrier_seen:
            raise DomainError('ACCESS_BLOCKED', 'Security barriers prohibit automatic session recovery')
        if not self.browser.is_connected():
            raise DomainError('BROWSER_SESSION_LOST', 'Browser process stopped; explicit retry required')
        if self.profile.recovery_level == 'strong_recovery':
            await self.context.close()
            await self.new_page_context()
        else:
            if not self.page.is_closed():
                await self.page.close()
            self.page = await self.context.new_page()
            self.page.set_default_timeout(self.profile.element_ms)
            self.page.set_default_navigation_timeout(self.profile.navigation_ms)
            self.page.on('response', self._response)
        self.response_status = None
        self._planner_targets = {}

    async def __aexit__(self, *args):
        if self.slots is not None:
            try:
                if self.context:
                    await self.context.close()
            finally:
                await self.slots.release(self)
            return
        if self.manager:
            await self.manager.__aexit__(*args)

    async def _route(self, route):
        request = route.request
        p = urlsplit(request.url)
        if p.scheme not in {"https", "http"}:
            return await route.abort()
        host = p.hostname or ""
        if host not in self._public_hosts:
            try:
                addresses = await asyncio.to_thread(socket.getaddrinfo, host, p.port or 443)
                self._public_hosts[host] = bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
            except OSError:
                self._public_hosts[host] = False
        if not self._public_hosts[host]:
            return await route.abort()
        if request.is_navigation_request() and request.frame == self.page.main_frame and host not in self.allowed_hosts:
            return await route.abort()
        await route.continue_()

    async def _response(self, response):
        if response.request.is_navigation_request() and response.frame == self.page.main_frame:
            self.response_status = response.status
        if (response.status in {401, 403} and response.request.resource_type in {"document", "xhr", "fetch"}
                and urlsplit(response.url).hostname in self.allowed_hosts):
            self.response_status = response.status
            self.barrier_seen = True
        if response.status == 429:
            self.barrier_seen = True
            self.rate_limit_host = urlsplit(response.url).hostname
            self.response_status = 429
            raw = response.headers.get("retry-after")
            if raw:
                try:
                    until = now() + timedelta(seconds=max(0, int(raw)))
                except ValueError:
                    try:
                        until = parsedate_to_datetime(raw)
                        if until.tzinfo is None:
                            until = until.replace(tzinfo=now().tzinfo)
                    except (TypeError, ValueError):
                        until = now() + timedelta(seconds=60)
            else:
                until = now() + timedelta(seconds=60)
            self.retry_after = max(self.retry_after or until, until)

    async def navigate(self, url):
        if urlsplit(url).hostname not in self.allowed_hosts:
            raise DomainError("ACCESS_BLOCKED", "Recipe destination is outside its approved source")
        await self.page.goto(url, wait_until="domcontentloaded", timeout=self.profile["navigation_ms"])

    async def security(self) -> Inspection | None:
        result = await self._inspect_security()
        if result:
            self.barrier_seen = True
        return result

    async def _inspect_security(self) -> Inspection | None:
        if not self.alive:
            raise DomainError("BROWSER_SESSION_LOST", "Udaan Browser session closed")
        if self.retry_after and now() < self.retry_after:
            return Inspection(state=PageState.CHALLENGE, challenge_type="RATE_LIMIT",
                              reason="Source rate limit requires operator wait", retry_after=self.retry_after)
        # A completed cooldown permits read-only DOM validation. It does not authorize navigation.
        # A still-visible rate-limit page remains a barrier below.
        if self.response_status in {401, 403} or (self.response_status == 429 and not self.retry_after):
            kind = {401: "AUTHENTICATION", 403: "ACCESS_RESTRICTED", 429: "RATE_LIMIT"}[self.response_status]
            return Inspection(state=PageState.CHALLENGE, challenge_type=kind,
                              reason=f"Source returned HTTP {self.response_status}; manual action required")
        markers = [
            ('iframe[title*="challenge" i], iframe[title*="captcha" i], [id="captcha"], [class="g-recaptcha"], [class="h-captcha"]', "CAPTCHA", "Security challenge detected"),
            ('input[type="password"]', "AUTHENTICATION", "Visible authentication form requires manual action"),
        ]
        for selector, kind, reason in markers:
            matches = self.page.locator(selector)
            for i in range(min(await matches.count(), 10)):
                if await matches.nth(i).is_visible():
                    return Inspection(state=PageState.CHALLENGE, challenge_type=kind, reason=reason)
        # Read only enough visible text to classify a barrier; never persist it.
        body = (await self.page.locator("body").inner_text(timeout=3000))[:15000].lower()
        for pattern, kind, reason in [
            (r"verify (?:that )?you are (?:a )?human|complete the captcha", "CAPTCHA", "Human verification required"),
            (r"access denied|you have been blocked|automated access.*(?:denied|prohibited)", "ACCESS_RESTRICTED", "Source explicitly restricts access"),
            (r"too many requests|rate limit exceeded", "RATE_LIMIT", "Source rate limit is active"),
            (r"(?:sign|log) in to continue|session (?:has )?expired.*(?:sign|log) in", "AUTHENTICATION", "Authentication is required to continue"),
            (r"checking your browser|please wait.*security check", "INTERSTITIAL", "Security interstitial requires operator attention"),
        ]:
            if re.search(pattern, body):
                return Inspection(state=PageState.CHALLENGE, challenge_type=kind, reason=reason)
        if re.search(r"manual (?:operator )?action (?:is )?required|select your country to continue", body):
            return Inspection(state=PageState.MANUAL_ACTION_REQUIRED,
                              reason="Page explicitly requests manual operator action")
        return None

    async def visible(self, selector):
        locator = self.page.locator(selector)
        for index in range(min(await locator.count(), 30)):
            if await locator.nth(index).is_visible():
                return locator.nth(index)
        return None

    async def structure(self):
        """Allowlisted element structure only; no text, values, URLs, or challenge contents."""
        if await self.security():
            return None
        return await self.page.locator("main, [role=main], body").first.evaluate("""root =>
            Array.from(root.querySelectorAll('article, section, div, span, button')).slice(0, 600)
              .map(e => ({tag:e.tagName.toLowerCase(), role:['main','button','list','listitem','table','row','cell','grid','gridcell'].includes(e.getAttribute('role')) ? e.getAttribute('role') : null,
                classes:Array.from(e.classList).filter(x => /^[a-zA-Z][a-zA-Z_-]{0,50}$/.test(x)).slice(0,4)}))
        """)


    async def recipe_context(self):
        """Travel control structure only. Never read input values or arbitrary page text."""
        if await self.security():
            return None
        elements = await self.page.locator("body").evaluate(r"""root => {
            const documentRoot = root;
            const form = Array.from(root.querySelectorAll('form')).find(e => e.getClientRects().length && e.querySelector('input:not([type=hidden]):not([type=password])'));
            if (form) root = form;
            else {
                const fields=Array.from(root.querySelectorAll('input')).filter(e => e.getClientRects().length && /origin|destination|departure/i.test(e.getAttribute('aria-label') || ''));
                if (fields.length >= 2) {
                    let common=fields[0].parentElement;
                    while(common && !fields.every(e => common.contains(e))) common=common.parentElement;
                    while(common && common !== root && common.querySelectorAll('button,[role=combobox]').length < 4) common=common.parentElement;
                    if(common) root=common;
                }
            }
            const rows=[], ids=new Map();
            const safe = value => value && /^[a-zA-Z][a-zA-Z_-]{0,49}$/.test(value) && !/token|secret|password|auth|captcha|session/i.test(value);
            const labels = /^(?:select |enter |choose |open |bookingWidget[.])?(?:origin(?: airport)?|destination(?: airport)?|from|to|depart(?:ure)?(?: date)?|return(?: date)?|date picker|class|cabin|passengers?|adults?|children|infants?|search(?: flights)?|find flights|one way|round trip|accept all|done|apply)$/i;
            for (const e of [root, ...root.querySelectorAll('main,form,section,article,div,label,input,select,option,button,span'), ...Array.from(documentRoot.querySelectorAll('button')).filter(e => /^accept all$/i.test(e.textContent.trim()))]) {
                if (rows.length >= 350) break;
                if (e.matches('input[type=password],input[type=hidden]') || e.closest('[id*=captcha i],[class*=captcha i],iframe')) continue;
                if (!e.getClientRects().length) continue;
                let parent=e.parentElement; while(parent && !ids.has(parent)) parent=parent.parentElement;
                const label=(e.getAttribute('aria-label') || (e.labels && e.labels[0] && e.labels[0].textContent) || (e.matches('button,label,option,span') ? e.textContent : '') || '').trim();
                const item={tag:e.tagName.toLowerCase(),parent:ids.has(parent)?ids.get(parent):null,
                    classes:Array.from(e.classList).filter(safe).slice(0,6)};
                if(safe(e.id)) item.id=e.id;
                if(labels.test(label)) item.label=label;
                const aria=e.getAttribute('aria-label'); if(aria && labels.test(aria)) item.aria_label=aria;
                const role=e.getAttribute('role'); if(['main','button','combobox','listbox','option','textbox','spinbutton'].includes(role)) item.role=role;
                const type=e.getAttribute('type'); if(['text','date','number','submit','button','radio'].includes(type)) item.type=type;
                ids.set(e,rows.length); rows.push(item);
            }
            return rows;
        }""")
        if await self.security():
            return None
        return elements

    async def page_state(self, request, goal, scope=None, expanded=False):
        """Compact semantic context. Private locators/values never go to the model."""
        from udaan.config import public_url
        if await self.security():
            return None
        self._planner_request = request
        root = self.page.locator(scope).first if scope else self.page.locator('body')
        if scope:
            cards=self.page.locator(scope)
            for index in range(min(await cards.count(),100)):
                candidate=cards.nth(index)
                if await candidate.is_visible():
                    content=await candidate.inner_text()
                    if all(re.search(r'\b'+code+r'\b',content) for code in [request.origin,request.destination]):
                        root=candidate
                        break
        raw = await root.evaluate(r"""root => {
            const visible=e=>!!(e.getClientRects().length) && getComputedStyle(e).visibility!=='hidden';
            const occluded=e=>{const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;if(x<0||y<0||x>=innerWidth||y>=innerHeight)return false;const hit=document.elementFromPoint(x,y);return !!hit && hit!==e && !e.contains(hit);};
            const safe=s=>s && /^[a-zA-Z][a-zA-Z_-]{0,49}$/.test(s) && !/token|auth|session|captcha|secret|password/i.test(s);
            const stable=s=>safe(s)&&!/(?:^|[-_]|[a-z])(?:Focused|Selected|Active|Hover|Today|Disabled)$|^(focus|focused|active|selected|checked|disabled|open|show|hidden|hover|today|is-active|is-focused|is-selected|is-today|is-hovered)$/i.test(s);
            const path=e=>{
                if(e===root)return ':scope';
                for(const attr of ['data-testid','id','name','aria-label','placeholder','data-date','datetime']){
                    const v=e.getAttribute(attr);
                    if(v && /^[a-zA-Z0-9][a-zA-Z0-9 ,:_\/-]{0,80}$/.test(v) && !/token|auth|session|captcha|secret|password/i.test(v)){
                        const css=e.tagName.toLowerCase()+'['+attr+'="'+v+'"]';
                        if(root.querySelectorAll(css).length===1)return css;
                    }
                }
                const classes=Array.from(e.classList).filter(stable).slice(0,4);
                if(classes.length){const css=e.tagName.toLowerCase()+classes.map(x=>'.'+x).join('');if(root.querySelectorAll(css).length===1)return css;}
                const parts=[];let n=e;
                while(n && n!==root && parts.length<35){
                    if(n!==e){
                        const parentClasses=Array.from(n.classList).filter(stable).slice(0,4);
                        if(parentClasses.length){const prefix=n.tagName.toLowerCase()+parentClasses.map(x=>'.'+x).join('');if(root.querySelectorAll(prefix).length===1)return prefix+' > '+parts.join(' > ');}
                    }
                    let index=1;for(let p=n.previousElementSibling;p;p=p.previousElementSibling)if(p.tagName===n.tagName)index++;
                    parts.unshift(n.tagName.toLowerCase()+':nth-of-type('+index+')');n=n.parentElement;
                }
                return n===root?parts.join(' > '):'';
            };
            const calendarDate=e=>{
                const months=['January','February','March','April','May','June','July','August','September','October','November','December'];
                const pattern=new RegExp('\\b('+months.join('|')+')\\s+(20[0-9]{2})\\b','g');
                const dateValue=e.getAttribute('data-date')||e.getAttribute('datetime')||e.getAttribute('aria-label')||'';
                const iso=dateValue.match(/\b(20[0-9]{2}-[0-9]{2}-[0-9]{2})\b/);
                if(iso)return iso[1];
                const full=dateValue.match(new RegExp('('+months.join('|')+') ([0-9]{1,2})(?:st|nd|rd|th)?,? (20[0-9]{2})'));
                if(full)return full[3]+'-'+String(months.indexOf(full[1])+1).padStart(2,'0')+'-'+full[2].padStart(2,'0');
                const text=e.textContent.trim();
                if(!/^(?:[1-9]|[12][0-9]|3[01])$/.test(text)||!e.matches('button,td,[role=gridcell],[role=button]'))return '';
                for(let n=e.parentElement,level=0;n&&level<7;n=n.parentElement,level++){
                    const headers=[...n.innerText.matchAll(pattern)];
                    const unique=new Map(headers.map(m=>[m[0],m]));
                    if(unique.size>1)return '';
                    if(unique.size===1){const m=[...unique.values()][0];return m[2]+'-'+String(months.indexOf(m[1])+1).padStart(2,'0')+'-'+text.padStart(2,'0');}
                }
                return '';
            };
            const query='input,button,select,[role=button],[role=option],[role=radio],[role=tab],[role=gridcell],[tabindex],label,td,span,p,h1,h2,h3,h4,h5,h6,time,strong,b,small';
            let scanRoot=root;
            if(root.tagName==='BODY'){
                const all=Array.from(root.querySelectorAll('input,[role=button],label')).filter(visible);
                const semantic=e=>[e.getAttribute('aria-label'),e.getAttribute('placeholder'),e.className,e.tagName==='LABEL'?e.textContent:''].join(' ');
                const from=all.find(e=>/origin|__from|\bFrom\b/i.test(semantic(e)));
                const to=all.find(e=>/destination|__to|\bTo\b/i.test(semantic(e)));
                if(from&&to){let n=from.parentElement;while(n&&!n.contains(to))n=n.parentElement;if(n)scanRoot=n.closest('form')||n.parentElement||n;}
            }
            const elements=Array.from(scanRoot.querySelectorAll(query));
            if(root.tagName!=='BODY')elements.push(...root.querySelectorAll('div,img[alt]'));
            for(const dialog of root.querySelectorAll('[role=dialog],[role=listbox],[class*=calendar],[class*=datepicker],[class*=popover]'))if(visible(dialog))elements.push(...dialog.querySelectorAll(query));
            for(const consent of root.querySelectorAll('button,[role=button]'))if(visible(consent)&&/^(accept all|accept|close|search|search flights|find flights|done|apply)$/i.test((consent.getAttribute('aria-label')||consent.textContent).trim()))elements.push(consent);
            const unique=Array.from(new Set(elements));
            return unique.filter(e=>!(e.matches('td,[role=gridcell]') && Array.from(e.querySelectorAll('button,[role=button]')).some(visible))).filter(e=>!e.parentElement?.closest('button,[role=option]')).filter(e=>visible(e) && !e.closest('[autocomplete=current-password], [autocomplete=username]') && !['password','hidden','email','tel'].includes(e.type))
                .slice(0,1200).map(e=>({occluded:occluded(e),calendar_date:calendarDate(e),in_control:e.matches('input,button,select,label,td,[role=button],[role=option],[role=radio],[role=tab],[role=gridcell],[tabindex]')||!!e.closest('button,label,[role=button],[role=option],[class*=calendar]'),selector:path(e),tag:e.tagName.toLowerCase(),role:e.getAttribute('role')||'',
                label:(e.getAttribute('aria-label')||(e.labels&&e.labels[0]?.textContent)||e.querySelector('label,[class*=label]')?.textContent||(e.matches('input')?e.closest('[role=button][aria-label]')?.getAttribute('aria-label'):'')||'').replace(/\s+/g,' ').trim().slice(0,160),
                text:(e.matches('input,select')?'':e.tagName==='IMG'?e.getAttribute('alt'):e.innerText||'').replace(/\s+/g,' ').trim().slice(0,180),
                placeholder:e.getAttribute('placeholder')||'',name:e.getAttribute('name')||'',
                nearby:(e.parentElement?.innerText||'').replace(/\s+/g,' ').trim().slice(0,120),
                id:safe(e.id)?e.id:'',testid:safe(e.getAttribute('data-testid'))?e.getAttribute('data-testid'):'',
                classes:Array.from(e.classList).filter(safe).slice(0,4),type:e.type||'',readonly:e.readOnly===true,
                enabled:!e.disabled && !e.closest('[aria-disabled=true]') && !Array.from(e.classList).some(c=>/(?:Disabled|Passive|OutsideMonth)$/.test(c)),visible:true}));
        }""")
        # Preserve only travel vocabulary, airport codes, dates/times and monetary tokens.
        # Arbitrary names, addresses and account content cannot enter prompts or memory.
        words = r'from|to|origin|destination|departure|depart|arrival|return|date|search|flight|flights|one|way|round|trip|adult|adults|child|children|infant|infants|passenger|passengers|traveller|travellers|economy|premium|business|first|cabin|class|select|choose|airport|airports|city|next|previous|month|year|calendar|done|apply|accept|all|cookies|continue|close|book|now|IndiGo|Air|India|Express|Akasa|SpiceJet|Alliance|Star|FLY91|IndiaOne|total|fare|price|INR|USD|EUR|GBP|AED|SGD|January|February|March|April|May|June|July|August|September|October|November|December|Mon|Tue|Wed|Thu|Fri|Sat|Sun'
        def clean(value):
            if re.search(r'password|sign.?in|log.?in|captcha|verify|payment|card number|account|otp|token', value, re.I):
                return ''
            return ' '.join(re.findall(r'(?i:\b(?:'+words+r')\b)|\b[A-Z]{3}\b|\b[A-Z0-9]{2}\s?\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}\b|[₹$€£]?\b\d[\d,]*(?:\.\d{1,2})?\b', value))[:160]
        items, targets = [], {}
        for node in raw:
            if not scope and not node.pop('in_control', False):
                continue
            node.pop('in_control', None)
            if re.search(r'view all|learn more|explore|swiper|media-flip|skipContent|skip to', node['text']+' '+node['label']+' '+node['id']+' '+' '.join(node['classes']),re.I):
                continue
            # Reject restricted controls even when surrounding text contains travel words.
            if re.search(r'password|captcha|auth|login|sign.?in|payment|checkout|purchase|delete|refund|cancel.booking|subscribe|email|phone|contact|account', ' '.join(str(node.get(k,'')) for k in ['label','name','placeholder','id','text']), re.I):
                continue
            for key in ['label','text','placeholder','name','nearby']:
                node[key] = clean(node[key])
            meaningful = ' '.join(node[k] for k in ['label','text','placeholder','name'])
            if not meaningful.strip() or not node['selector'] or len(node['selector']) > 500:
                continue
            ident = 'e'+str(len(items)+1)
            targets[ident] = {**node, 'scope': scope}
            public_keys = ['tag','role','label','text','placeholder','name','nearby','type','readonly','visible','enabled','occluded']
            if scope:
                public_keys = ['tag','label','text','nearby','role']
            if node.get('calendar_date'):
                public_keys = ['tag','calendar_date','enabled','visible','occluded']
            items.append({'target_id': ident, **{k:node[k] for k in public_keys if node.get(k) or isinstance(node.get(k),bool)}})
        terms = {'origin':['from','origin'], 'destination':['to','destination'], 'departure_date':['date','depart','calendar','month'], 'adults':['adult','passenger','traveller'], 'cabin':['cabin','class','economy'], 'submit':['search','flight']}.get(goal, [goal.replace('_',' ')])
        def score(x):
            text=' '.join(str(x.get(k,'')) for k in ['label','text','placeholder','name']).lower()
            field_score=0
            if scope:
                compact=len(text)<70
                if goal in {'total_fare','currency'} and re.search(r'₹|\b(?:inr|usd|eur)\b',text):
                    field_score=40+(20 if compact else 0)
                elif goal=='airline' and re.search(r'indigo|air india|akasa|spicejet|alliance|star air|fly91|indiaone',text):
                    field_score=30+(40 if len(text)<30 else 0)
                elif goal=='flight_number' and re.search(r'\b[a-z0-9]{2}\s?\d{2,4}\b',text):
                    field_score=40+(20 if compact else 0)
                elif goal in {'departure_time','arrival_time'} and re.search(r'\b\d{1,2}:\d{2}\b',text):
                    field_score=30+(10 if compact else 0)
                elif goal in {'origin','destination'} and re.search(r'\b'+getattr(request,goal).lower()+r'\b',text):
                    field_score=40+(20 if compact else 0)
            return field_score + (50 if goal=='departure_date'  and x.get('calendar_date')==str(request.departure_date) else 0) + sum(5 for t in terms if re.search(r'\b'+t+r'\b',text)) + (2 if x['tag'] in {'input','button','select'} or x.get('role') else 0)
        items.sort(key=score,reverse=True)
        items=items[:24 if scope else (80 if expanded else 40)]
        self._planner_targets = {x['target_id']: targets[x['target_id']] for x in items}
        if await self.security():
            return None
        return {'url':public_url(self.page.url), 'title':clean(await self.page.title()), 'goal':goal, 'elements':items}

    async def planner_value_matches(self, selector, request, field):
        target = self.page.locator(selector)
        if await target.count()!=1 or not await target.is_visible():
            return False
        raw = await target.evaluate("e => ['INPUT','SELECT'].includes(e.tagName)?e.value:((e.getAttribute('aria-label')||'')+' '+(e.innerText||''))")
        raw = ' '.join(raw.split())
        value = str(getattr(request, field))
        if field == 'departure_date':
            day=request.departure_date
            return any(re.search(r'(?<!\d)'+re.escape(v)+r'(?!\d)',raw,re.I) for v in [day.isoformat(),day.strftime('%d %b %Y'),day.strftime('%d %B %Y'),day.strftime('%d/%m/%Y'),str(day.day)+day.strftime(' %B %Y'),str(day.day)+day.strftime(' %b %Y'),day.strftime('%B ')+str(day.day)+day.strftime(', %Y')])
        if field in {'origin','destination'}:
            # A dropdown containing many airport suggestions is not a selected airport.
            codes=set(re.findall(r'\b[A-Z]{3}\b',raw))
            return codes=={value}
        return bool(re.search(r'\b'+re.escape(value)+r'\b', raw, re.I))

    async def planner_execute(self, learned, request, guard):
        """Execute one constrained, observed search action and verify its actual effect."""
        await guard()
        if await self.security():
            raise DomainError('RECIPE_ACTION_REJECTED','The page requires operator action.')
        if learned.calendar_day:
            await self.page_state(request, 'departure_date', expanded=True)
            dates=[n for n in self._planner_targets.values() if n.get('calendar_date')==str(request.departure_date) and n['enabled']]
            if len(dates)!=1:
                return False, 'The requested calendar date is not uniquely visible and enabled.'
            selector=dates[0]['selector']
        else:
            selector=learned.selector
        target=self.page.locator(selector)
        if await target.count()!=1 or not await target.is_visible() or not await target.is_enabled():
            return False, 'The selected control is no longer uniquely visible and enabled.'
        kind=await target.get_attribute('type')
        if kind in {'password','hidden','email','tel'}:
            raise DomainError('RECIPE_ACTION_REJECTED','Restricted input cannot be used by a recipe.')
        label=(await target.get_attribute('aria-label') or await target.inner_text() or await target.get_attribute('placeholder') or learned.label or '')[:200]
        if re.search(r'captcha|verify|sign.?in|log.?in|password|payment|purchase|delete|refund|cancel.booking|subscribe|account|otp',label,re.I):
            raise DomainError('RECIPE_ACTION_REJECTED','The selected control is outside flight search.')
        if learned.action == 'click' and not re.search(r'from|\bto\b|origin|destination|airport|search|flight|one way|round trip|date|depart|arrival|return|calendar|month|next|previous|adult|child|infant|traveller|passenger|cabin|economy|business|first|accept|cookie|done|apply|close|\b[A-Z]{3}\b|\b\d{1,4}\b', label, re.I):
            raise DomainError('RECIPE_ACTION_REJECTED','The selected click is not a recognized flight-search control.')
        before_url=self.page.url
        before=await self.page.locator('body').evaluate("e=>e.innerText")
        value=learned.value
        if value and value.startswith('{{'):
            field=value[2:-2]
            if field!=learned.goal:
                raise DomainError('RECIPE_ACTION_REJECTED','Input parameter does not match the current goal.')
            value=str(getattr(request,field))
        try:
            if learned.action=='fill':
                await target.fill(value,timeout=5000)
            elif learned.action=='select':
                await target.select_option(label=value,timeout=5000)
            elif learned.action=='click':
                await target.click(timeout=5000)
            elif learned.action=='press':
                await target.press(value,timeout=5000)
            elif learned.action=='scroll':
                await target.scroll_into_view_if_needed(timeout=3000)
            elif learned.action=='wait_visible':
                await target.wait_for(state='visible',timeout=5000)
            elif learned.action in {'wait_text','extract_text','extract_money'}:
                text=await target.inner_text(timeout=3000)
                if learned.action=='extract_money':
                    from udaan.eura import money
                    money(text)
            elif learned.action=='wait_url':
                await self.page.wait_for_url(lambda url:str(url)!=before_url,timeout=5000)
        except DomainError:
            raise
        except Exception as exc:
            await guard()
            if 'intercepts pointer events' in str(exc):
                return False, 'Another visible panel covers this control. Finish its Continue, Done, Apply or Close action before retrying.'
            if self.page.url != before_url:
                return True, 'Navigation started; the result state still requires verification.'
            return False,'The action did not complete within its bounded wait.'
        await asyncio.sleep(.6)
        await guard()
        if learned.calendar_day:
            ok = False
            for _ in range(5):
                await self.page_state(request, 'departure_date', expanded=True)
                for node in self._planner_targets.values():
                    if node.get('calendar_date'):
                        continue  # A visible day is not proof of a selected date.
                    semantic = ' '.join(node.get(k,'') for k in ['label','placeholder','name','nearby'])
                    if re.search(r'depart|date',semantic,re.I) and await self.planner_value_matches(node['selector'],request,'departure_date'):
                        ok = True
                        break
                if ok:
                    break
                await asyncio.sleep(.3)
                await guard()
        elif learned.action in {'fill','select'}:
            ok=await self.planner_value_matches(learned.selector,request,learned.goal)
        elif learned.action in {'wait_visible','scroll','extract_text','extract_money'}:
            ok=await target.is_visible()
        else:
            after=await self.page.locator('body').evaluate("e=>e.innerText")
            ok=before_url!=self.page.url or before!=after
            if not ok and learned.goal!='submit':
                ok=await self.planner_value_matches(learned.selector,request,learned.goal)
        return ok, 'Verified browser effect.' if ok else 'No expected browser change was observed.'

    async def result_candidates(self):
        if await self.security():
            return []
        candidates=await self.page.locator('body').evaluate(r"""root=>{
            const groups=new Map();
            for(const e of root.querySelectorAll('article,section,div,li')){
                if(!e.getClientRects().length || e.innerText.length>6000)continue;
                const classes=Array.from(e.classList).filter(s=>/^[a-zA-Z][a-zA-Z_-]{1,40}$/.test(s));
                if(!classes.length)continue;
                const selector=e.tagName.toLowerCase()+classes.slice(0,3).map(s=>'.'+s).join('');
                if(groups.has(selector))continue;
                const nodes=Array.from(root.querySelectorAll(selector)).filter(n=>n.getClientRects().length);
                if(nodes.length<2||nodes.length>100)continue;
                const valid=nodes.filter(n=>/(?:INR|₹|USD|EUR)\s*[\d,]+|[\d,]+\s*(?:INR|USD|EUR)/.test(n.textContent)&&/\b[A-Z0-9]{2}\s?\d{2,4}\b/.test(n.innerText)&&(n.innerText.match(/\b[0-9]{1,2}:[0-9]{2}\b/g)||[]).length>=2);
                if(valid.length===nodes.length)groups.set(selector,{selector,tag:e.tagName.toLowerCase(),repeated:nodes.length,size:e.textContent.length});
            }
            return Array.from(groups.values()).sort((a,b)=>a.size-b.size).slice(0,5);
        }""")
        self._result_candidates={f'c{i+1}':v for i,v in enumerate(candidates)}
        return [{'target_id':k,'tag':v['tag'],'repeated':v['repeated']} for k,v in self._result_candidates.items()]

    async def airport_options(self, selector):
        """Read actual enabled native options, including custom-styled select backing data."""
        if await self.security():
            raise DomainError('OPERATOR_ACTION_REQUIRED', 'Airport discovery paused at a security barrier')
        target = self.page.locator(selector)
        if await target.count() != 1:
            raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'Airport selector is not unique')
        return await target.locator('option').evaluate_all("es => es.filter(e => !e.disabled && /^[A-Z]{3}$/.test(e.value)).map(e => ({iata:e.value, label:e.text.trim()}))")

    def watch_airport_catalog(self, spec):
        """Observe only the configured public airport response; never replay requests."""
        self.airport_catalog = None
        self._airport_tasks = set()
        async def receive(response):
            configured = urlsplit(spec.catalog_url)
            actual = urlsplit(response.url)
            if (actual.scheme, actual.netloc, actual.path) != (configured.scheme, configured.netloc, configured.path) or response.status != 200:
                return
            try:
                if int(response.headers.get('content-length', '0')) > 8_000_000:
                    return
                payload = await response.json()
                for key in spec.catalog_path:
                    payload = payload[key]
                if not isinstance(payload, list) or not 1 <= len(payload) <= 5000:
                    return
                airports = []
                for item in payload:
                    if not isinstance(item, dict):
                        return
                    if item.get('inActive') is True or item.get('allowed') is False:
                        continue
                    projected = {key: item.get(value) for key, value in spec.catalog_fields.items()}
                    if not re.fullmatch(r'[A-Z]{3}', projected.get('iata') or ''):
                        continue
                    if projected.get('country') != 'IN':
                        continue
                    from udaan.discovery import DiscoveredAirport
                    airports.append(DiscoveredAirport.model_validate(projected))
                if airports:
                    self.airport_catalog = airports
            except Exception:
                self.airport_catalog = None
        def listener(response):
            task = asyncio.create_task(receive(response))
            self._airport_tasks.add(task)
            task.add_done_callback(self._airport_tasks.discard)
        self.page.on('response', listener)
