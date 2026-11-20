# -*- coding: utf-8 -*-
"""Исторический генератор черновика каталога (данные на 09.11.2026).

По умолчанию пишет только data/catalog-draft.json, не заменяя рабочий каталог:
его правила и список площадок устарели. Чтобы указать другой путь, используйте
--output; перед ручной заменой рабочего каталога необходимо проверить все данные.

Поля услуги:
  amount         сколько единиц даёт один заход
  cooldown_min   пауза между заходами (мин)
  max_claims_day сколько заходов в сутки (None = без заявленного лимита)
  hold_seconds   сколько секунд нужно держать вкладку/ждать таймер
  url            точная страница с бесплатной кнопкой
  confirmed      True  — цифры видел лично на сайте 09.11.2026
                 False — из свежих обзоров (проверять вручную)
"""
import argparse
import json
import pathlib

S = []


def site(**kw):
    S.append(kw)


def svc(**kw):
    kw.setdefault("confirmed", True)
    return kw


# ---------------------------------------------------------------- ВК и другие площадки
site(id="smm8", name="SMM8", home="https://smm8.com", free_page="https://smm8.com/free-services",
     platforms=["vk", "telegram", "instagram", "youtube", "tiktok", "x", "facebook"],
     reg_required=False, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="Международная панель. Самый большой суточный лимит по ВК. Держать вкладку до конца таймера.",
     services=[
         svc(id="smm8-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=30, max_claims_day=48,
             hold_seconds=120, url="https://smm8.com/free-vk-followers", label="Подписчики ВК (профиль/группа/паблик)"),
         svc(id="smm8-vk-likes", platform="vk", type="likes", amount=10, cooldown_min=30, max_claims_day=48,
             hold_seconds=120, url="https://smm8.com/free-vk-likes", label="Лайки ВК"),
         svc(id="smm8-vk-views", platform="vk", type="views", amount=100, cooldown_min=30, max_claims_day=48,
             hold_seconds=120, url="https://smm8.com/free-vk-video-views", label="Просмотры видео ВК"),
         svc(id="smm8-tg-followers", platform="telegram", type="followers", amount=10, cooldown_min=30, max_claims_day=48,
             hold_seconds=120, url="https://smm8.com/free-telegram-members", label="Подписчики Telegram"),
         svc(id="smm8-yt-followers", platform="youtube", type="followers", amount=10, cooldown_min=30, max_claims_day=48,
             hold_seconds=120, url="https://smm8.com/free-youtube-subscribers", label="Подписчики YouTube"),
     ])

site(id="prskill", name="PRSkill", home="https://prskill.ru", free_page="https://prskill.ru/#nakrutka-besplatno",
     platforms=["vk", "telegram", "instagram", "tiktok", "youtube", "twitch", "likee", "twitter", "facebook", "soundcloud"],
     reg_required=False, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="Раздел «Бесплатная накрутка без заданий»: каждые 30 минут, число заказов не ограничено.",
     services=[
         svc(id="prskill-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=30, max_claims_day=48,
             hold_seconds=60, url="https://prskill.ru/services/vk-vstupivshie#giveaway-form", label="Подписчики ВК (вступившие)"),
         svc(id="prskill-vk-likes", platform="vk", type="likes", amount=25, cooldown_min=30, max_claims_day=48,
             hold_seconds=60, url="https://prskill.ru/services/vk-lajki#giveaway-form", label="Лайки на запись ВК"),
         svc(id="prskill-vk-views", platform="vk", type="views", amount=100, cooldown_min=30, max_claims_day=48,
             hold_seconds=60, url="https://prskill.ru/services/nakrutka-prosmotrov-v-vk-na-zapisi#giveaway-form",
             label="Просмотры записи ВК"),
         svc(id="prskill-tg-followers", platform="telegram", type="followers", amount=10, cooldown_min=30, max_claims_day=48,
             hold_seconds=60, url="https://prskill.ru/services/telegram-podpischiki-na-kanal#giveaway-form",
             label="Подписчики Telegram"),
         svc(id="prskill-tg-reactions", platform="telegram", type="reactions", amount=10, cooldown_min=30, max_claims_day=48,
             hold_seconds=60, url="https://prskill.ru/services/telegram-nakrutka-reaktsiy#giveaway-form",
             label="Реакции Telegram"),
         svc(id="prskill-yt-likes", platform="youtube", type="likes", amount=20, cooldown_min=30, max_claims_day=48,
             hold_seconds=60, url="https://prskill.ru/services/youtube-lajki#giveaway-form", label="Лайки YouTube"),
     ])

site(id="smmmedia", name="SMM.media", home="https://smm.media", free_page="https://smm.media",
     platforms=["vk", "telegram", "instagram", "tiktok", "youtube", "twitch", "likee", "twitter", "traffic"],
     reg_required=False, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="~70 бесплатных услуг. В один слот (20 минут) — ОДНА услуга на выбор.",
     services=[
         svc(id="smmmedia-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=20, max_claims_day=72,
             hold_seconds=30, url="https://smm.media/podpisciki-v-vk#free_boost", label="Подписчики ВК (СНГ)"),
         svc(id="smmmedia-vk-views", platform="vk", type="views", amount=100, cooldown_min=20, max_claims_day=72,
             hold_seconds=30, url="https://smm.media/kupit-prosmotry-v-vk-na-zapisi#free_boost", label="Просмотры поста ВК"),
         svc(id="smmmedia-vk-video", platform="vk", type="views", amount=100, cooldown_min=20, max_claims_day=72,
             hold_seconds=30, url="https://smm.media/nakrutit-prosmotry-v-vk-na-video-ili-klipy#free_boost",
             label="Просмотры видео/клипов ВК"),
         svc(id="smmmedia-tg-followers", platform="telegram", type="followers", amount=15, cooldown_min=20, max_claims_day=72,
             hold_seconds=30, url="https://smm.media/podpischiki-telegram#free_boost", label="Подписчики Telegram"),
         svc(id="smmmedia-tg-reactions", platform="telegram", type="reactions", amount=50, cooldown_min=20, max_claims_day=72,
             hold_seconds=30, url="https://smm.media/reakcii-v-telegram#free_boost", label="Реакции Telegram"),
         svc(id="smmmedia-ig-likes", platform="instagram", type="likes", amount=100, cooldown_min=20, max_claims_day=72,
             hold_seconds=30, url="https://smm.media/kupit-laiki-v-instagram#free_boost", label="Лайки Instagram"),
     ])

site(id="lowcostsmm", name="LowcostSMM", home="https://lowcostsmm.com", free_page="https://lowcostsmm.com/ru/free/",
     platforms=["vk", "telegram", "instagram", "youtube", "tiktok", "max", "rutube", "twitter", "facebook", "dzen"],
     reg_required=False, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="35 бесплатных услуг; по ВК самый широкий набор. Не закрывать вкладку 5 минут, 1 раз в сутки на услугу.",
     services=[
         svc(id="lowcostsmm-vk-followers", platform="vk", type="followers", amount=20, cooldown_min=1440, max_claims_day=1,
             hold_seconds=300, url="https://lowcostsmm.com/ru/free/nakrutka-podpischikov-vk-besplatno/",
             label="Подписчики ВК (страница)"),
         svc(id="lowcostsmm-vk-group", platform="vk", type="followers", amount=20, cooldown_min=1440, max_claims_day=1,
             hold_seconds=300, url="https://lowcostsmm.com/ru/free/nakrutka-podpischikov-v-gruppu-vk-besplatno/",
             label="Подписчики в группу ВК"),
         svc(id="lowcostsmm-vk-friends", platform="vk", type="friends", amount=15, cooldown_min=1440, max_claims_day=1,
             hold_seconds=300, url="https://lowcostsmm.com/ru/free/nakrutka-druzey-vk-besplatno/", label="Друзья ВК"),
         svc(id="lowcostsmm-vk-likes", platform="vk", type="likes", amount=25, cooldown_min=1440, max_claims_day=1,
             hold_seconds=300, url="https://lowcostsmm.com/ru/free/nakrutka-laykov-vk-besplatno/", label="Лайки ВК (посты)"),
         svc(id="lowcostsmm-vk-views", platform="vk", type="views", amount=100, cooldown_min=1440, max_claims_day=1,
             hold_seconds=300, url="https://lowcostsmm.com/ru/free/nakrutka-prosmotrov-vk-besplatno/",
             label="Просмотры постов ВК"),
         svc(id="lowcostsmm-vk-reposts", platform="vk", type="reposts", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=300, url="https://lowcostsmm.com/ru/free/nakrutka-repostov-vk-besplatno/", label="Репосты ВК"),
         svc(id="lowcostsmm-vk-plays", platform="vk", type="plays", amount=50, cooldown_min=1440, max_claims_day=1,
             hold_seconds=300, url="https://lowcostsmm.com/ru/free/nakrutka-proslushivaniy-vk-besplatno/",
             label="Прослушивания ВК"),
         svc(id="lowcostsmm-vk-comments", platform="vk", type="comments", amount=5, cooldown_min=1440, max_claims_day=1,
             hold_seconds=300, url="https://lowcostsmm.com/ru/free/nakrutka-kommentariev-vk-besplatno/",
             label="Комментарии ВК"),
         svc(id="lowcostsmm-tg-followers", platform="telegram", type="followers", amount=20, cooldown_min=1440, max_claims_day=1,
             hold_seconds=300, url="https://lowcostsmm.com/ru/free/nakrutka-podpischikov-telegram-besplatno/",
             label="Подписчики Telegram"),
     ])

site(id="smmnakrutka", name="SMMnakrutka", home="https://smmnakrutka.ru", free_page="https://smmnakrutka.ru",
     platforms=["vk", "telegram", "max", "instagram", "tiktok", "youtube", "rutube", "twitch"],
     reg_required=False, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="Таймер 5 минут на странице. Подписчики ВК — раз в 5 часов, просмотры — раз в 3 часа.",
     services=[
         svc(id="smmnakrutka-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=300, max_claims_day=5,
             hold_seconds=300, url="https://smmnakrutka.ru/free-vk-followers", label="Подписчики ВК"),
         svc(id="smmnakrutka-vk-likes", platform="vk", type="likes", amount=10, cooldown_min=300, max_claims_day=5,
             hold_seconds=300, url="https://smmnakrutka.ru/free-vk-likes", label="Лайки ВК"),
         svc(id="smmnakrutka-vk-views", platform="vk", type="views", amount=100, cooldown_min=180, max_claims_day=5,
             hold_seconds=300, url="https://smmnakrutka.ru/free-vk-views", label="Просмотры поста ВК"),
         svc(id="smmnakrutka-vk-video", platform="vk", type="views", amount=200, cooldown_min=180, max_claims_day=5,
             hold_seconds=300, url="https://smmnakrutka.ru/free-vk-video-views", label="Просмотры видео ВК"),
         svc(id="smmnakrutka-vk-plays", platform="vk", type="plays", amount=50, cooldown_min=300, max_claims_day=5,
             hold_seconds=300, url="https://smmnakrutka.ru/free-vk-plays", label="Прослушивания ВК"),
         svc(id="smmnakrutka-tg-followers", platform="telegram", type="followers", amount=10, cooldown_min=300, max_claims_day=5,
             hold_seconds=300, url="https://smmnakrutka.ru/free-telegram-followers", label="Подписчики Telegram"),
         svc(id="smmnakrutka-max-followers", platform="max", type="followers", amount=5, cooldown_min=300, max_claims_day=5,
             hold_seconds=300, url="https://smmnakrutka.ru/free-max-followers", label="Подписчики MAX"),
     ])

site(id="doctorsmm", name="DoctorSMM", home="https://doctorsmm.com",
     free_page="https://doctorsmm.com/nakrutka-podpischikov-vk-platno/",
     platforms=["vk", "telegram", "instagram", "youtube", "tiktok", "max", "ok", "rutube", "twitch", "dzen"],
     reg_required=False, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="Кнопка «Попробовать бесплатно»: тестовый заказ до 50 единиц, ежедневно, без регистрации, ~10 минут.",
     services=[
         svc(id="doctorsmm-vk-followers", platform="vk", type="followers", subject="community", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=600, url="https://doctorsmm.com/nakrutka/nakrutka-podpischikov-v-gruppu-vk-besplatno/",
             label="Подписчики сообщества ВК (тест до 10)"),
         svc(id="doctorsmm-vk-likes", platform="vk", type="likes", amount=50, cooldown_min=1440, max_claims_day=1,
             hold_seconds=600, url="https://doctorsmm.com/nakrutka-lajkov-vk-platno-i-deshevo/", label="Лайки ВК (тест)"),
         svc(id="doctorsmm-tg-followers", platform="telegram", type="followers", amount=50, cooldown_min=1440,
             max_claims_day=1, hold_seconds=600, url="https://doctorsmm.com/nakrutka-podpischikov-v-telegramm-kanal/",
             label="Подписчики Telegram (тест)"),
     ])

site(id="likeboom", name="LikeBoom", home="https://likeboom.ru", free_page="https://likeboom.ru/free-trial",
     platforms=["vk", "telegram", "max", "tiktok", "instagram", "youtube", "twitter", "twitch", "facebook", "rutube", "dzen"],
     reg_required=False, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="Бесплатный заказ до 15 ₽, таймер 2 минуты, 1 заказ в день. Дневной лимит разбирают — заходить лучше утром.",
     services=[
         svc(id="likeboom-vk-followers", platform="vk", type="followers", amount=50, cooldown_min=1440, max_claims_day=1,
             hold_seconds=120, url="https://likeboom.ru/free-trial", label="Любая услуга до 15 ₽ (≈50 подписчиков ВК)"),
         svc(id="likeboom-vk-likes", platform="vk", type="likes", amount=50, cooldown_min=1440, max_claims_day=1,
             hold_seconds=120, url="https://likeboom.ru/free-trial", label="Лайки ВК (в рамках 15 ₽)"),
         svc(id="likeboom-vk-views", platform="vk", type="views", amount=100, cooldown_min=1440, max_claims_day=1,
             hold_seconds=120, url="https://likeboom.ru/free-trial", label="Просмотры ВК (в рамках 15 ₽)"),
     ])

site(id="avi1", name="Avi1", home="https://avi1.ru", free_page="https://avi1.ru",
     platforms=["vk", "telegram", "instagram", "youtube", "tiktok", "max", "dzen"],
     reg_required=False, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="Каждая услуга 1 раз в сутки. Таймер ~10 минут, приём заявок 08:00–22:00 МСК. Боты, часть отписывается.",
     services=[
         svc(id="avi1-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=600, url="https://avi1.ru/piar/nakrutka-podpischikov-v-gruppu-vk-besplatno/",
             label="Подписчики в группу ВК"),
         svc(id="avi1-vk-friends", platform="vk", type="friends", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=600, url="https://avi1.ru/nakrutka-druzej-vk/", label="Друзья ВК"),
         svc(id="avi1-vk-likes", platform="vk", type="likes", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=600, url="https://avi1.ru/nakrutka-lajkov-vk-platno/", label="Лайки ВК"),
         svc(id="avi1-vk-views", platform="vk", type="views", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=600, url="https://avi1.ru/nakrutka-prosmotrov-vkontakte-platno/", label="Просмотры ВК"),
     ])

site(id="nakruti", name="Nakruti.net", home="https://nakruti.net", free_page="https://nakruti.net/besplatnaya-nakrutka",
     platforms=["vk", "telegram", "instagram", "tiktok", "youtube", "rutube", "threads"],
     reg_required=False, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="Без заданий и пароля. Повтор через 5 минут, но не более 3 оплат в сутки по услуге.",
     services=[
         svc(id="nakruti-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=5, max_claims_day=3,
             hold_seconds=60, url="https://nakruti.net/besplatnaya-nakrutka/besplatnye-podpischiki-vk", label="Подписчики ВК"),
         svc(id="nakruti-vk-friends", platform="vk", type="friends", amount=10, cooldown_min=5, max_claims_day=3,
             hold_seconds=60, url="https://nakruti.net/besplatnaya-nakrutka/besplatnye-druzya-vk", label="Друзья ВК"),
         svc(id="nakruti-vk-likes", platform="vk", type="likes", amount=10, cooldown_min=5, max_claims_day=3,
             hold_seconds=60, url="https://nakruti.net/besplatnaya-nakrutka/besplatnye-layki-vk", label="Лайки ВК"),
         svc(id="nakruti-vk-views", platform="vk", type="views", amount=100, cooldown_min=5, max_claims_day=3,
             hold_seconds=60, url="https://nakruti.net/besplatnaya-nakrutka/besplatnye-prosmotry-vk", label="Просмотры ВК"),
         svc(id="nakruti-vk-plays", platform="vk", type="plays", amount=100, cooldown_min=5, max_claims_day=3,
             hold_seconds=60, url="https://nakruti.net/besplatnaya-nakrutka/besplatnye-proslushivaniya-vk",
             label="Прослушивания ВК"),
         svc(id="nakruti-vk-reposts", platform="vk", type="reposts", amount=5, cooldown_min=5, max_claims_day=3,
             hold_seconds=60, url="https://nakruti.net/besplatnaya-nakrutka/besplatnye-reposty-vk", label="Репосты ВК"),
         svc(id="nakruti-vk-comments", platform="vk", type="comments", amount=5, cooldown_min=5, max_claims_day=3,
             hold_seconds=60, url="https://nakruti.net/besplatnaya-nakrutka/besplatnye-kommentarii-vk",
             label="Комментарии ВК"),
         svc(id="nakruti-tg-followers", platform="telegram", type="followers", amount=10, cooldown_min=5, max_claims_day=3,
             hold_seconds=60, url="https://nakruti.net/besplatnaya-nakrutka", label="Подписчики Telegram"),
     ])

site(id="smmcode", name="SMMCode", home="https://smmcode.shop", free_page="https://smmcode.shop",
     platforms=["vk", "telegram", "instagram", "tiktok", "youtube", "twitch", "rutube", "twitter", "ok", "max"],
     reg_required=False, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="Бонусная накрутка по таймеру очереди. Промокоды на бесплатную накрутку раздают в Telegram-канале ежедневно.",
     services=[
         svc(id="smmcode-vk-followers", platform="vk", type="followers", subject="person", amount=10, cooldown_min=45, max_claims_day=8,
             hold_seconds=60, url="https://smmcode.shop/nakrutka/vkontakte/podpischikov-na-akkaunt", label="Подписчики ВК"),
         svc(id="smmcode-vk-likes", platform="vk", type="likes", amount=10, cooldown_min=45, max_claims_day=8,
             hold_seconds=60, url="https://smmcode.shop/nakrutka/vkontakte/laikov", label="Лайки ВК"),
         svc(id="smmcode-vk-views", platform="vk", type="views", amount=100, cooldown_min=45, max_claims_day=8,
             hold_seconds=60, url="https://smmcode.shop/nakrutka/vkontakte/prosmotry-na-zapisi", label="Просмотры записей ВК"),
     ])

site(id="mrpopular", name="MRPOPULAR", home="https://ru.mrpopular.net", free_page="https://ru.mrpopular.net",
     platforms=["vk", "ok", "telegram", "youtube"],
     reg_required=False, verified="site+reviews", mode="assisted", enabled=True,
     notes="Раздел «Бесплатное продвижение»: выдача ~15 минут, повтор через 5 минут с ростом ожидания.",
     services=[
         svc(id="mrpopular-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=15, max_claims_day=6,
             hold_seconds=60, url="https://ru.mrpopular.net/?category=socialnetworks&type=vk&service=subscribers",
             label="Подписчики ВК", confirmed=False),
         svc(id="mrpopular-vk-likes", platform="vk", type="likes", amount=10, cooldown_min=15, max_claims_day=6,
             hold_seconds=60, url="https://ru.mrpopular.net/", label="Лайки ВК", confirmed=False),
         svc(id="mrpopular-vk-views", platform="vk", type="views", amount=100, cooldown_min=15, max_claims_day=6,
             hold_seconds=60, url="https://ru.mrpopular.net/", label="Просмотры ВК", confirmed=False),
     ])

site(id="smmchik", name="SMMchik", home="https://smmchik.ru", free_page="https://smmchik.ru",
     platforms=["vk", "telegram", "instagram", "tiktok", "youtube", "max", "twitch", "likee", "facebook", "twitter", "dzen"],
     reg_required=True, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="Весь каталог «от 0.00 ₽», но на бесплатном тарифе нужно выполнять задания. Регистрация обязательна.",
     services=[
         svc(id="smmchik-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=60, max_claims_day=3,
             hold_seconds=60, url="https://smmchik.ru/vk/subscribers", label="Подписчики ВК", confirmed=False),
         svc(id="smmchik-vk-likes", platform="vk", type="likes", amount=10, cooldown_min=60, max_claims_day=3,
             hold_seconds=60, url="https://smmchik.ru/vk/vklikes/", label="Лайки ВК", confirmed=False),
         svc(id="smmchik-vk-plays", platform="vk", type="plays", amount=50, cooldown_min=60, max_claims_day=3,
             hold_seconds=60, url="https://smmchik.ru/vk/vkmusiclisten/", label="Прослушивания ВК", confirmed=False),
         svc(id="smmchik-vk-views", platform="vk", type="views", amount=100, cooldown_min=60, max_claims_day=3,
             hold_seconds=60, url="https://smmchik.ru/vk/vkviews/", label="Просмотры ВК", confirmed=False),
     ])

site(id="smmprime", name="SMMPrime", home="https://smmprime.com", free_page="https://smmprime.com/free",
     platforms=["vk", "telegram", "instagram", "tiktok", "youtube", "twitch", "max", "ok", "dzen", "rutube"],
     reg_required=True, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="После регистрации: 10 лайков + 100 просмотров, слоты пополняются каждый час. Подписчиков ВК бесплатно нет.",
     services=[
         svc(id="smmprime-vk-likes", platform="vk", type="likes", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=30, url="https://smmprime.com/free", label="Лайки (10 шт. после регистрации)"),
         svc(id="smmprime-vk-views", platform="vk", type="views", amount=100, cooldown_min=1440, max_claims_day=1,
             hold_seconds=30, url="https://smmprime.com/free", label="Просмотры (100 шт. после регистрации)"),
     ])

site(id="likest", name="Likest", home="https://likest.ru", free_page="https://likest.ru",
     platforms=["vk"],
     reg_required=False, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="20 бесплатных лайков на любое фото ВК (акция). Вход через лайк/статус ВК, пароль не нужен.",
     services=[
         svc(id="likest-vk-likes", platform="vk", type="likes", amount=20, cooldown_min=1440, max_claims_day=1,
             hold_seconds=30, url="https://likest.ru/", label="20 лайков на фото ВК"),
     ])

site(id="likeinsta", name="LikeInsta", home="https://likeinsta.io", free_page="https://likeinsta.io",
     platforms=["instagram", "vk", "ok", "tiktok", "telegram"],
     reg_required=False, verified="site-2026-11-09", mode="assisted", enabled=True,
     notes="Бесплатные лайки без регистрации: чем дольше открыта вкладка, тем больше. ВК — только за баллы с заданий.",
     services=[
         svc(id="likeinsta-ig-likes", platform="instagram", type="likes", amount=50, cooldown_min=5, max_claims_day=20,
             hold_seconds=300, url="https://likeinsta.io/", label="Лайки Instagram (за время сессии)"),
         svc(id="likeinsta-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=60, max_claims_day=2,
             hold_seconds=60, url="https://likeinsta.io/login/", label="Подписчики ВК (за баллы с заданий)"),
     ])

# ---- сервисы, где бесплатное подтверждено только обзорами
site(id="taplike", name="TapLike", home="https://taplike.ru", free_page="https://taplike.ru",
     platforms=["vk", "telegram", "instagram", "youtube", "ok"],
     reg_required=True, verified="reviews", mode="assisted", enabled=True,
     notes="Бонусные (бесплатные) тарифы для зарегистрированных, 1 раз в сутки. Проверять в личном кабинете.",
     services=[
         svc(id="taplike-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=60, url="https://taplike.ru/", label="Подписчики ВК (бонусный тариф)", confirmed=False),
     ])

site(id="prmotion", name="PR Motion", home="https://prmotion.ru", free_page="https://prmotion.ru",
     platforms=["vk", "telegram", "instagram", "youtube", "tiktok"],
     reg_required=True, verified="reviews", mode="assisted", enabled=True,
     notes="Бесплатная тестовая накрутка: подписчики приходят через 7 минут после заявки.",
     services=[
         svc(id="prmotion-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=60, url="https://prmotion.ru/", label="Подписчики ВК (тест)", confirmed=False),
     ])

site(id="nupago", name="Nupago", home="https://nupago.ru", free_page="https://nupago.ru",
     platforms=["vk", "telegram", "instagram", "youtube"],
     reg_required=True, verified="reviews", mode="assisted", enabled=True,
     notes="Бесплатные бонусы новым пользователям + автоматическое выполнение заказов.",
     services=[
         svc(id="nupago-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=60, url="https://nupago.ru/", label="Подписчики ВК (бонус)", confirmed=False),
     ])

site(id="feijoa", name="Feijoa", home="https://feijoa.pro", free_page="https://feijoa.pro",
     platforms=["vk", "telegram", "instagram", "youtube", "tiktok", "ok", "rutube", "dzen", "twitch"],
     reg_required=True, verified="reviews", mode="assisted", enabled=True,
     notes="Бонусная рулетка каждые 15 минут: бесплатные услуги и рубли на баланс. Гарантия докрутки до 360 дней.",
     services=[
         svc(id="feijoa-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=15, max_claims_day=30,
             hold_seconds=30, url="https://feijoa.pro/", label="Подписчики ВК (бонусы рулетки)", confirmed=False),
     ])

site(id="spetsnakrutka", name="Spetsnakrutka", home="https://spetsnakrutka.ru", free_page="https://spetsnakrutka.ru",
     platforms=["vk", "telegram", "instagram", "youtube"],
     reg_required=False, verified="reviews", mode="assisted", enabled=True,
     notes="Бесплатный тест и промокоды, заказ без регистрации, минимальный заказ от 10 подписчиков.",
     services=[
         svc(id="spetsnakrutka-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=1440,
             max_claims_day=1, hold_seconds=60, url="https://spetsnakrutka.ru/", label="Подписчики ВК (тест)",
             confirmed=False),
     ])

site(id="masslike", name="Masslike", home="https://masslike.ru", free_page="https://masslike.ru",
     platforms=["vk", "telegram", "instagram", "youtube"],
     reg_required=False, verified="reviews", mode="assisted", enabled=True,
     notes="Бесплатный тестовый объём, заказ без регистрации.",
     services=[
         svc(id="masslike-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=60, url="https://masslike.ru/", label="Подписчики ВК (тест)", confirmed=False),
     ])

site(id="bestsmm", name="BestSMM", home="https://bestsmm.pro", free_page="https://bestsmm.pro",
     platforms=["vk", "telegram", "instagram", "youtube", "tiktok"],
     reg_required=False, verified="reviews", mode="assisted", enabled=True,
     notes="Бесплатный тест без регистрации (достаточно ссылки и e-mail).",
     services=[
         svc(id="bestsmm-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=60, url="https://bestsmm.pro/", label="Подписчики ВК (тест)", confirmed=False),
     ])

site(id="smmyt", name="SMMYT", home="https://smmyt.ru", free_page="https://smmyt.ru",
     platforms=["vk", "telegram", "youtube", "instagram"],
     reg_required=True, verified="reviews", mode="assisted", enabled=True,
     notes="Бесплатный тариф + приветственный бонус.",
     services=[
         svc(id="smmyt-vk-followers", platform="vk", type="followers", amount=10, cooldown_min=1440, max_claims_day=1,
             hold_seconds=60, url="https://smmyt.ru/", label="Подписчики ВК (бесплатный тариф)", confirmed=False),
     ])

# ---- сервис без реальной бесплатной накрутки: показываем честно, отключён
site(id="777like", name="777Like", home="https://777like.ru", free_page="https://777like.ru",
     platforms=["vk", "telegram", "youtube", "twitch", "kick", "tiktok", "instagram", "twitter", "rutube"],
     reg_required=True, verified="site-2026-11-09", mode="manual", enabled=False,
     notes="27 бесплатных услуг открываются ТОЛЬКО после первого пополнения; подписчиков ВК в этом списке нет.",
     services=[])

catalog = {
    "meta": {
        "version": "2026.11.09",
        "source": "проверено вручную 09.11.2026 + обзоры vc.ru / otzyvmarketing / dtf",
        "note": "Лимиты соответствуют публичным условиям сервисов на дату. Владельцы сайтов меняют их без предупреждения.",
    },
    "types": {
        "followers": "Подписчики",
        "likes": "Лайки",
        "views": "Просмотры",
        "reposts": "Репосты",
        "comments": "Комментарии",
        "friends": "Друзья",
        "plays": "Прослушивания",
        "reactions": "Реакции",
        "traffic": "Трафик на сайт",
    },
    "platforms": {
        "vk": "ВКонтакте", "telegram": "Telegram", "instagram": "Instagram", "tiktok": "TikTok",
        "youtube": "YouTube", "twitch": "Twitch", "likee": "Likee", "twitter": "X (Twitter)",
        "facebook": "Facebook", "ok": "Одноклассники", "max": "MAX", "rutube": "Rutube",
        "dzen": "Дзен", "threads": "Threads", "soundcloud": "SoundCloud", "x": "X (Twitter)",
        "traffic": "Сайт",
    },
    "sites": S,
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Создать исторический черновик каталога")
    parser.add_argument("--output", type=pathlib.Path,
                        default=pathlib.Path(__file__).parent / "data" / "catalog-draft.json",
                        help="куда записать черновик (по умолчанию data/catalog-draft.json)")
    out = parser.parse_args().output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(s["services"]) for s in S)
    print("Исторический черновик: сайтов: %d, услуг: %d -> %s (%d байт)" %
          (len(S), total, out, out.stat().st_size))
