"""The mail providers anyone can sign up at.

Granting one of these as a whole email domain would admit the whole
internet, so the desktop refuses such a grant and the connector never
surfaces one to a recipient; only individual addresses at them can be
granted. One list, so the two sides can never disagree about which domains
are public.
"""

from typing import Final

PUBLIC_EMAIL_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.ca",
        "yahoo.co.id",
        "yahoo.co.in",
        "yahoo.co.jp",
        "yahoo.co.nz",
        "yahoo.co.uk",
        "yahoo.com.ar",
        "yahoo.com.au",
        "yahoo.com.br",
        "yahoo.com.hk",
        "yahoo.com.mx",
        "yahoo.com.ph",
        "yahoo.com.sg",
        "yahoo.com.tw",
        "yahoo.com.vn",
        "yahoo.de",
        "yahoo.dk",
        "yahoo.es",
        "yahoo.fr",
        "yahoo.gr",
        "yahoo.ie",
        "yahoo.it",
        "yahoo.nl",
        "yahoo.no",
        "yahoo.pl",
        "yahoo.se",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "gmx.com",
        "gmx.net",
        "mail.com",
        "yandex.com",
        "zoho.com",
        "qq.com",
        "163.com",
        "126.com",
        "naver.com",
        "hey.com",
        "fastmail.com",
        "tuta.io",
    }
)
