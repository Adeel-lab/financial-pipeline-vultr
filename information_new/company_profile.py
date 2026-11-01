"""
company_profile.py - ONE place that defines "which company, where" for every stage.

A profile = company name + optional locations + optional tickers/aliases/excludes.
  --location omitted        -> no geographic restriction (same as global)
  --location global         -> explicitly worldwide scope
  --location Budapest       -> local scope, one location
  --location Washington California "New York"   -> local scope, several locations
Tickers and aliases are optional. Private companies simply have no ticker.

Used by discovery.py (builds searches + filters), and read back from the saved JSON by
triage.py, verify.py, sentiment.py and swot.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path

GLOBAL_WORDS = {"global", "worldwide", "world", "international"}
LEGAL_SUFFIXES = {"inc", "corp", "corporation", "ltd", "limited", "llc", "plc", "gmbh", "ag", "sa",
                  "nv", "co", "company", "kft", "zrt", "bv", "oy", "ab"}
GENERIC_QUERY_TOPICS = ("earnings", "lawsuit", "regulation", "acquisition", "partnership",
                        "layoffs", "launch", "CEO")
DEFAULT_EDITION = "hl=en-US&gl=US&ceid=US:en"


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def slugify(text: str) -> str:
    t = re.sub(r"[^a-z0-9]+", "-", strip_accents(text).lower()).strip("-")
    return t or "company"


def _quote(term: str) -> str:
    return f'"{term}"' if re.search(r"\s", term) else term


def strip_legal_suffix(name: str) -> str:
    words = name.replace(",", " ").split()
    while len(words) > 1 and words[-1].lower().rstrip(".") in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


@dataclass(frozen=True)
class CompanyProfile:
    name: str
    scope: str = "global"                       # "global" or "local"
    locations: tuple[str, ...] = ()
    tickers: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()               # regex patterns
    extra_queries: tuple[str, ...] = ()
    strict_location: bool = False
    edition: str = DEFAULT_EDITION
    scope_explicit: bool = False                # user typed "global" (vs. simply omitted)

    @property
    def label(self) -> str:
        """Display name used as the 'company' key in every output file."""
        if self.scope == "local" and self.locations:
            return f"{self.name} ({', '.join(self.locations)})"
        return self.name

    @property
    def slug(self) -> str:
        return slugify(self.label)

    @property
    def location_group(self) -> str:
        if self.scope != "local" or not self.locations:
            return ""
        return "(" + " OR ".join(_quote(l) for l in self.locations) + ")"

    @property
    def search_name(self) -> str:
        """Name used in searches: legal suffix dropped ('Microsoft Corporation' -> 'Microsoft')."""
        return strip_legal_suffix(self.name)

    @property
    def query(self) -> str:
        return f'"{self.search_name}" {self.location_group}'.strip()

    @property
    def all_aliases(self) -> tuple[str, ...]:
        out, seen = [], set()
        for a in (self.name, strip_legal_suffix(self.name), *self.aliases):
            k = a.strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(a.strip())
        return tuple(out)

    @property
    def all_extra_queries(self) -> tuple[str, ...]:
        if self.extra_queries:
            return self.extra_queries
        topics = list(GENERIC_QUERY_TOPICS) + (["stock"] if self.tickers else [])
        return tuple(f'"{self.search_name}" {t} {self.location_group}'.strip() for t in topics)

    def scope_text(self) -> str:
        if self.scope == "local" and self.locations:
            return ", ".join(self.locations)
        return "global (whole company)"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(label=self.label, slug=self.slug, query=self.query, scope_text=self.scope_text(),
                 all_aliases=list(self.all_aliases))
        return d


def parse_locations(values) -> tuple[str, tuple[str, ...], bool]:
    """-> (scope, locations, scope_explicit)"""
    locs: list[str] = []
    for v in values or []:
        for part in str(v).split(";"):
            if part.strip():
                locs.append(part.strip())
    if not locs:
        return "global", (), False
    if any(l.lower() in GLOBAL_WORDS for l in locs):
        if len(locs) > 1:
            raise ValueError("'global' cannot be combined with specific locations - use one or the other.")
        return "global", (), True
    seen, uniq = set(), []
    for l in locs:
        if l.lower() not in seen:
            seen.add(l.lower())
            uniq.append(l)
    return "local", tuple(uniq), True


def make_profile(name, location=None, ticker=None, alias=None, exclude=None,
                 extra_query=None, strict_location=False, edition=None) -> CompanyProfile:
    if not name or not str(name).strip():
        raise ValueError("Company name is required.")
    as_list = lambda v: [v] if isinstance(v, str) else list(v or [])
    scope, locs, explicit = parse_locations(as_list(location))
    return CompanyProfile(
        name=str(name).strip(), scope=scope, locations=locs,
        tickers=tuple(t.strip() for t in as_list(ticker) if t.strip()),
        aliases=tuple(a.strip() for a in as_list(alias) if a.strip()),
        exclude=tuple(re.escape(e.strip()) for e in as_list(exclude) if e.strip()),
        extra_queries=tuple(q.strip() for q in as_list(extra_query) if q.strip()),
        strict_location=bool(strict_location), edition=(edition or DEFAULT_EDITION).lstrip("?&"),
        scope_explicit=explicit)


# Optional shortcuts that reproduce the three companies used during development.
PRESETS = {
    "tesla": CompanyProfile(
        name="Tesla", tickers=("TSLA",),
        exclude=(r"Nikola Tesla", r"Tesla coil", r"Tesla Laboratories", r"Tesla-logo pills"),
        extra_queries=("Tesla stock", "Tesla Musk", "Tesla earnings", "Tesla FSD robotaxi",
                       "Tesla sales deliveries", "Tesla recall")),
    "gm": CompanyProfile(
        name="General Motors", tickers=("GM",), exclude=(r"genetically modified",),
        extra_queries=("GM earnings", "GM stock", "GM Chevrolet", "GM recall")),
    "ford": CompanyProfile(
        name="Ford Motor", aliases=("Ford",),
        exclude=(r"Harrison Ford", r"Gerald Ford", r"Gerald R\. Ford", r"Ford County", r"Tom Ford",
                 r"Betty Ford", r"Glenn Ford", r"Ford Foundation", r"\bTamiami Ford\b",
                 r"Ford dealership", r"Ford dealer\b"),
        extra_queries=("Ford F-150", "Ford stock", "Ford earnings", "Ford recall", "Ford EV")),
}


def add_profile_args(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group("company (any company - tickers/aliases/locations are optional)")
    g.add_argument("--company", help='Company name, e.g. "Betson Group" or "Microsoft"')
    g.add_argument("--location", nargs="+", default=None,
                   help='"global", or one or more places, e.g. Budapest  |  Washington California "New York"')
    g.add_argument("--ticker", action="append", default=[], help="optional stock ticker (repeatable)")
    g.add_argument("--alias", action="append", default=[], help="optional other name used in headlines (repeatable)")
    g.add_argument("--exclude", action="append", default=[], help="optional phrase meaning 'wrong entity' (repeatable)")
    g.add_argument("--extra-query", action="append", default=[], help="optional extra search for busy days (repeatable)")
    g.add_argument("--strict-location", action="store_true",
                   help="keep only articles whose headline/summary mentions a chosen location")
    g.add_argument("--edition", default=None,
                   help='Google News edition; default "hl=en-US&gl=US&ceid=US:en" (Hungary: "hl=hu&gl=HU&ceid=HU:hu")')
    g.add_argument("--companies-file", type=Path, help="JSON file with a list of companies (batch run)")
    g.add_argument("--preset", nargs="+", choices=list(PRESETS), help="built-in shortcuts: tesla gm ford")


def profiles_from_args(args) -> list[CompanyProfile]:
    profiles: list[CompanyProfile] = []
    try:
        if args.company:
            profiles.append(make_profile(args.company, args.location, args.ticker, args.alias, args.exclude,
                                         args.extra_query, args.strict_location, args.edition))
        elif args.location or args.ticker or args.alias:
            raise ValueError("--location/--ticker/--alias need --company.")
        if args.companies_file:
            data = json.loads(args.companies_file.read_text(encoding="utf-8-sig"))
            for row in (data["companies"] if isinstance(data, dict) else data):
                profiles.append(make_profile(
                    row["name"], row.get("location"), row.get("ticker", row.get("tickers")),
                    row.get("alias", row.get("aliases")), row.get("exclude"),
                    row.get("extra_queries"), row.get("strict_location", False), row.get("edition")))
        for key in args.preset or []:
            profiles.append(PRESETS[key])
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as e:
        sys.exit(f"Company input problem: {e}")
    if not profiles:
        sys.exit('No company given. Example: python discovery.py --company "Betson Group" --location Budapest')
    slugs = [p.slug for p in profiles]
    if len(set(slugs)) != len(slugs):
        sys.exit("Two companies resolve to the same name/location - make them different.")
    return profiles


def profile_from_dict(d, fallback_name: str = "") -> dict:
    """Tolerant reader used by later stages (older runs have no profile)."""
    if isinstance(d, dict) and d.get("name"):
        return d
    return {"name": fallback_name, "label": fallback_name, "scope": "global", "locations": [],
            "tickers": [], "aliases": [], "all_aliases": [fallback_name] if fallback_name else [],
            "scope_text": "global (whole company)"}


def distinctive_words(profile: dict) -> set[str]:
    """Words that identify the company itself (ignored when comparing a headline with a page)."""
    words: set[str] = set()
    for src in [profile.get("name", "")] + list(profile.get("all_aliases", [])) + list(profile.get("tickers", [])):
        for w in re.findall(r"\w+", strip_accents(src).lower()):
            words.add(w)
    return words


def location_hits(profile: dict, text: str) -> list[str]:
    if profile.get("scope") != "local":
        return []
    low = strip_accents(text or "").lower()
    return [l for l in profile.get("locations", []) if strip_accents(l).lower() in low]
