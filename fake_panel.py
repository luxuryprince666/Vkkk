# -*- coding: utf-8 -*-
"""Локальные панели-двойники для тестов драйверов.

Настоящие площадки нельзя дёргать в тестах: забанить могут, да и результат непредсказуем.
Здесь поднимается крошечный сервер, который ведёт себя как разные бесплатные панели:

    /panel        обычная форма, форма рисуется скриптом, ответ «Заказ принят»
    /timer        таймер: кнопка живая только через 3 секунды
    /captcha      форма с капчей (recaptcha-подобный блок)
    /captcha-after капча появляется только после нажатия кнопки (как Turnstile у smm8)
    /modal        кнопка заказа открывает «Важно знать…» (как nakruti): заказ после «Ок»
    /tomorrow     «на сегодня использована, возвращайтесь завтра» (лимит до завтра)
    /quota        отвечает «Лимит на сегодня исчерпан»
    /badlink      отвечает «Неверная ссылка»
    /email        требует e-mail перед заказом
    /broken       страница без формы вообще (форма «изменилась»)
    /plain        форма в чистом HTML без JS (для драйвера без браузера)
    /plain-submit приём формы /plain

Запуск вручную:  python tests/fake_panel.py  → адрес в консоли
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Бесплатная накрутка ВК</title></head>
<body style="font-family:sans-serif;max-width:640px;margin:40px auto">
<h3>Бесплатные подписчики ВКонтакте</h3>
{body}
<div id="result" style="margin-top:16px;color:#177245"></div>
<script>
{script}
</script>
</body></html>"""

FORM = """
<div id="f">
  <div><label for="link">Ссылка на профиль или сообщество</label><br>
  <input id="link" name="link" type="text" placeholder="https://vk.com/…" style="width:420px;padding:8px"></div>
  {extra}
  <div style="margin-top:12px"><button id="go" type="button" style="padding:9px 18px">Отправить заказ</button></div>
</div>
"""

SUBMIT_JS = """
document.getElementById('go').addEventListener('click', function () {
  const link = document.getElementById('link').value;
  setTimeout(function () {
    const r = document.getElementById('result');
    r.textContent = %(answer)s;
  }, 300);
});
"""

EMAIL_EXTRA = ('<div style="margin-top:8px"><label for="email">E-mail для связи</label><br>'
               '<input id="email" name="email" type="email" placeholder="you@mail.ru" '
               'style="width:280px;padding:8px"></div>')

COUNT_EXTRA = ('<div style="margin-top:8px"><label for="count">Количество</label><br>'
               '<input id="count" name="count" type="number" value="10" '
               'style="width:120px;padding:8px"></div>')


def page_panel() -> str:
    return PAGE.format(body=FORM.format(extra=COUNT_EXTRA),
                       script=SUBMIT_JS % {"answer": "'Заказ принят, ожидайте выполнения'"}).replace(
        "<h3>", "<h3>").replace('type="button"', 'type="button"')


def page_timer() -> str:
    body = FORM.format(extra="") + '<div id="tmr" style="margin-top:10px;color:#8a5a06">Подождите 3 сек</div>'
    script = """
const btn = document.getElementById('go');
btn.disabled = true;
let left = 3;
const t = setInterval(function () {
  left -= 1;
  document.getElementById('tmr').textContent = left > 0 ? ('Подождите ' + left + ' сек') : 'Можно заказывать';
  if (left <= 0) { clearInterval(t); btn.disabled = false; document.getElementById('tmr').textContent = ''; }
}, 1000);
""" + (SUBMIT_JS % {"answer": "'Заказ принят, ожидайте выполнения'"})
    return PAGE.format(body=body, script=script)


def page_captcha() -> str:
    body = FORM.format(extra="") + (
        '<div class="g-recaptcha" style="width:304px;height:78px;border:1px solid #ccc;'
        'margin-top:12px;padding:8px">Я не робот</div>')
    return PAGE.format(body=body, script=SUBMIT_JS % {"answer": "'Заказ принят'"})


AFTER_CAPTCHA_JS = """
const btn = document.getElementById('go');
btn.addEventListener('click', function () {
  // как Cloudflare Turnstile у smm8: капча приезжает ПОСЛЕ нажатия, с задержкой
  const box = document.createElement('div');
  box.className = 'cf-turnstile';
  box.style.cssText = 'width:300px;height:70px;border:1px solid #ccc;margin-top:12px';
  box.textContent = 'Подтвердите, что вы человек';
  const res = document.getElementById('result');
  setTimeout(function () { res.before(box); }, 900);
  res.textContent = 'Checking...';
});
"""


def page_captcha_after() -> str:
    """Форма без капчи, но капча появляется после нажатия — как у smm8 (Turnstile)."""
    return PAGE.format(body=FORM.format(extra=""), script=AFTER_CAPTCHA_JS)


MODAL_JS = """
document.getElementById('go').addEventListener('click', function () {
  // как у nakruti: вместо отправки выезжает «Важно знать перед запуском»
  const box = document.createElement('div');
  box.className = 'modal';
  box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.35);display:flex;' +
                      'align-items:center;justify-content:center;z-index:99';
  box.innerHTML = '<div style="background:#fff;padding:24px;border-radius:12px;width:360px">' +
                  '<b>Важно знать перед запуском</b>' +
                  '<p>Накрутка настоящая, но идёт в общей очереди.</p>' +
                  '<button class="btn-ok" type="button">Ок, понятно</button></div>';
  document.body.appendChild(box);
  box.querySelector('.btn-ok').addEventListener('click', function () {
    box.remove();
    document.getElementById('result').textContent = 'Заказ принят, ожидайте выполнения';
  });
});
"""


def page_modal() -> str:
    """Кнопка заказа сначала показывает модалку — заказ уходит только после «Ок, понятно»."""
    return PAGE.format(body=FORM.format(extra=""), script=MODAL_JS)


def page_answer(text: str, extra: str = "") -> str:
    return PAGE.format(body=FORM.format(extra=extra),
                       script=SUBMIT_JS % {"answer": repr(text)})


def page_email() -> str:
    return PAGE.format(body=FORM.format(extra=EMAIL_EXTRA),
                       script=SUBMIT_JS % {"answer": "'Заказ создан'"})


def page_cloudflare() -> str:
    """Заглушка Cloudflare: через 5 секунд «челлендж пройден» и открывается форма."""
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='5; url=/panel'>"
            "<title>Just a moment...</title></head><body>"
            "<h1>Just a moment...</h1><p>Checking your browser before accessing.</p>"
            "</body></html>")


def page_broken() -> str:
    return ("<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>Панель</title></head>"
            "<body><h3>Раздел временно недоступен</h3><p>Обновите страницу позже.</p></body></html>")


def page_plain() -> str:
    """Форма без JS: её умеет отправлять драйвер без браузера."""
    return """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Панель</title></head>
<body><h3>Бесплатные лайки</h3>
<form action="/plain-submit" method="post">
  <input type="hidden" name="token" value="abc123">
  <input name="link" type="text" placeholder="Ссылка на запись" style="width:400px;padding:8px">
  <input name="count" type="text" value="10" style="width:80px">
  <button type="submit" style="padding:8px 16px">Заказать</button>
</form></body></html>"""


# --------------------------------------------------------------- капчи: Turnstile и картинка

TURNSTILE_JS = """
document.getElementById('go').addEventListener('click', function () {
  const token = document.querySelector('[name=cf-turnstile-response]').value;
  const r = document.getElementById('result');
  if (!token) { r.textContent = 'Подтвердите, что вы не робот'; return; }
  r.textContent = 'Заказ принят, ожидайте выполнения';
});
"""

TURNSTILE_FRAME = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;font-family:sans-serif">
<div style="display:flex;align-items:center;gap:10px;width:300px;height:64px;border:1px solid #ccc;
            border-radius:6px;padding:0 10px;box-sizing:border-box">
  <input type="checkbox" id="checkbox" style="width:22px;height:22px"
         onchange="parent.document.querySelector('[name=cf-turnstile-response]').value='TS-TOKEN-OK'">
  <span style="font-size:14px">Подтвердите, что вы человек</span>
</div></body></html>"""


def page_turnstile() -> str:
    """Turnstile-подобная галочка внутри iframe: без клика заказ не проходит.

    iframe ведёт на путь, в котором есть «challenges.cloudflare» — драйвер опознаёт
    виджет по адресу фрейма, как у настоящего Cloudflare.
    """
    body = FORM.format(extra=(
        '<input type="hidden" name="cf-turnstile-response" value="">'
        '<div class="cf-turnstile" style="margin-top:12px;width:300px;height:64px">'
        '<iframe src="/cdn-cgi/challenges.cloudflare.com/turnstile" width="300" height="64" '
        'style="border:1px solid #eee;border-radius:6px"></iframe></div>'))
    return PAGE.format(body=body, script=TURNSTILE_JS)


def _render_captcha(code: str) -> bytes:
    """Рисуем текст капчи: сервер отдаёт PNG, как настоящая панель."""
    import io
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (140, 48), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
    except Exception:
        font = ImageFont.load_default()
    d.text((12, 6), code, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


CAPTCHA_CODES: dict[str, str] = {}          # token -> код (как сессия на настоящей панели)
_CAPTCHA_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _new_code() -> str:
    import random
    return "".join(random.choice(_CAPTCHA_ALPHABET) for _ in range(5))


def page_imgcaptcha() -> str:
    """Классическая капча: картинка с 5 символами + поле для кода."""
    body = FORM.format(extra=(
        '<div style="margin-top:10px" class="captcha-box">'
        '<img id="cap-img" class="captcha" src="/captcha-img?t=1" width="140" height="48" alt="captcha">'
        '<div style="margin-top:6px"><label for="captcha-code">Код с картинки</label><br>'
        '<input id="captcha-code" name="captcha-code" type="text" placeholder="код с картинки" '
        'style="width:140px;padding:8px"></div></div>'))
    script = """
let captchaToken = '1';
document.getElementById('go').addEventListener('click', function () {
  const code = document.getElementById('captcha-code').value.trim();
  const r = document.getElementById('result');
  fetch('/captcha-check?token=' + captchaToken + '&code=' + encodeURIComponent(code))
    .then((res) => res.json())
    .then((data) => {
      if (data.ok) { r.textContent = 'Заказ принят, ожидайте выполнения'; return; }
      captchaToken = data.next || captchaToken;
      document.getElementById('cap-img').src = '/captcha-img?t=' + captchaToken;
      document.getElementById('captcha-code').value = '';
      r.textContent = 'Неверный код, попробуйте снова';
    });
});
"""
    return PAGE.format(body=body, script=script)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):        # тишина в тестах
        pass

    def _send(self, html: str, code: int = 200) -> None:
        data = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/cdn-cgi/challenges.cloudflare.com/turnstile":
            return self._send(TURNSTILE_FRAME)
        if path == "/captcha-img":
            token = (parse_qs(self.path.split("?")[1]).get("t", ["1"])[0]
                     if "?" in self.path else "1")
            code = CAPTCHA_CODES.get(token) or _new_code()
            CAPTCHA_CODES[token] = code
            return self._send_bytes(_render_captcha(code), "image/png")
        if path == "/captcha-check":
            q = parse_qs(self.path.split("?")[1]) if "?" in self.path else {}
            token = (q.get("token") or ["1"])[0]
            code = (q.get("code") or [""])[0]
            ok = bool(CAPTCHA_CODES.get(token)) and code.upper() == str(CAPTCHA_CODES.get(token)).upper()
            nxt = token if ok else str(int(token) + 1 if token.isdigit() else "2")
            if not ok:
                CAPTCHA_CODES[nxt] = _new_code()
            return self._send_bytes(json.dumps({"ok": ok, "next": nxt}).encode(), "application/json")
        pages = {
            "/panel": page_panel,
            "/timer": page_timer,
            "/captcha": page_captcha,
            "/captcha-after": page_captcha_after,
            "/modal": page_modal,
            "/turnstile": page_turnstile,
            "/cloudflare": page_cloudflare,
            "/imgcaptcha": page_imgcaptcha,
            "/tomorrow": lambda: page_answer("Бесплатная накрутка на сегодня использована. "
                                             "Возвращайтесь завтра"),
            "/email": page_email,
            "/broken": page_broken,
            "/plain": page_plain,
            "/": page_panel,
        }
        if path == "/quota":
            return self._send(page_answer("Лимит на сегодня исчерпан, попробуйте позже"))
        if path == "/badlink":
            return self._send(page_answer("Неверная ссылка — проверьте адрес"))
        if path in pages:
            return self._send(pages[path]())
        self._send("<html><body>404</body></html>", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = {k: v[0] for k, v in parse_qs(raw).items()}
        if self.path.split("?")[0] == "/plain-submit":
            if form.get("link"):
                return self._send(page_answer("Заказ создан, ожидайте"))
            return self._send(page_answer("Неверная ссылка"))
        self._send("<html><body>ok</body></html>")


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    server, base = start(8765)
    print(f"панели-двойники: {base}/turnstile  {base}/imgcaptcha  {base}/panel  {base}/timer  {base}/captcha  {base}/quota  "
          f"{base}/badlink  {base}/email  {base}/broken  {base}/plain")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
