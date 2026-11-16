"""Tests for the Common Crawl Oracle Recruiting Cloud refresh, a maintenance tool
the engine never runs.

The rules are the other discovery tools': add only, and an employer is admitted
only when its own API answers with jobs. What is particular to Oracle, and pinned
here, is that a pod answers an unknown site number with its default site's jobs
(measured on BDO), so the crawl's sites are pruned to the distinct ones rather
than trusted.
"""
from __future__ import annotations

import asyncio
import collections
import json
import re
import sys
from urllib.parse import unquote

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_none

from hireshire import paths

sys.path.insert(0, str(paths.ROOT / "scripts"))

import discover_oracle as do  # noqa: E402

FAST_RETRY = retry(
    retry=retry_if_exception(lambda e: isinstance(e, httpx.HTTPStatusError)),
    stop=stop_after_attempt(2), wait=wait_none(), reraise=True,
)
POD = "ebqb.fa.us2.oraclecloud.com"


def _cdx(*urls: str) -> list[str]:
    return [json.dumps({"url": u}) for u in urls]


# --- Extraction -------------------------------------------------------------------


def test_a_pod_and_its_sites_come_off_the_candidate_experience_path():
    found = do.pairs_from_cdx(_cdx(
        f"https://{POD}/hcmUI/CandidateExperience/en/sites/CX_1/job/14715",
        f"https://{POD}/hcmUI/CandidateExperience/en/sites/CX_1/requisitions",
        f"https://{POD}/hcmUI/CandidateExperience/fr-CA/sites/BDOExperiencedCareers/jobs",
        f"https://{POD}/hcmUI/CandidateExperience/en/sites/CX_1",
        "https://EGUG.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en_US/sites/CX_1/job/9",
        f"https://{POD}/fscmUI/faces/FuseWelcome",                 # not a candidate site
        "https://careers.oracle.com/en/sites/jobsearch/jobs",       # a vanity front, not a pod
        "<html>504</html>",
    ))
    assert found == {
        POD: collections.Counter({"CX_1": 3, "BDOExperiencedCareers": 1}),
        "egug.fa.us2.oraclecloud.com": collections.Counter({"CX_1": 1}),
    }


def test_a_site_title_is_tidied_into_a_label():
    assert do.clean_label("BDO USA Experienced Career Site") == "BDO USA Experienced"
    assert do.clean_label("Careers at Pella") == "Pella"
    assert do.clean_label("Estes Express Lines - Careers") == "Estes Express Lines"
    assert do.clean_label("Welcome to Worthington Steel Jobs") == "Worthington Steel"
    assert do.clean_label("Oracle") == "Oracle"
    assert do.clean_label("Careers") is None


def test_oracles_own_default_titles_are_never_an_employer():
    # Read off the live crawl: sites that never set a title keep the template's.
    for title in ("Candidate Experience site", "Home Page", "Careers", "Job Search",
                  "Page not found", "Generic", "Global", "Work with us", "Career Site  - New Applicants"):
        assert do.clean_label(title) is None, title
    assert do.clean_label("Pearson Candidate Experience Site") == "Pearson"
    assert do.clean_label("Fife Council Jobs and Careers") == "Fife Council"
    for title, expected in [
        ("Jobs at Acosta", "Acosta"), ("Check out Arqiva", "Arqiva"),
        ("Trabaja en Interbank", "Interbank"),
        ("the Myriad Genetics Candidate site", "Myriad Genetics"),
        ("St. Croix County Career Section", "St. Croix County"),
        ("S&C Minimal", "S&C"), ("Copa Airlines - NEW", "Copa Airlines"),
        ("ACI Worldwide Job Opportunities", "ACI Worldwide"),
        ("McLeod Careers Section", "McLeod"),
        ("Pittsburg State University Career Site Minimal", "Pittsburg State University"),
        ("Work with us | Copa Airlines - NEW", "Copa Airlines"),
        ("Careers at Texas Woman&rsquo;s University | TWU", "Texas Woman’s University | TWU"),
        # Real names that merely contain a template word are left alone.
        ("Hilton Grand Vacations", "Hilton Grand Vacations"), ("APP Recruitment", "APP Recruitment"),
        ("The Fedcap Group", "The Fedcap Group"),
        ("Pella Windows & Doors", "Pella Windows & Doors"),
    ]:
        assert do.clean_label(title) == expected, title


def test_a_branded_site_number_names_its_employer_and_a_generated_one_does_not():
    assert do.label_from_site("AperamCareerSite") == "Aperam"
    assert do.label_from_site("CentrusEnergyCareers") == "CentrusEnergy"
    assert do.label_from_site("TForceFreight") == "TForceFreight"
    for site in ("CX", "CX_1001", "jobsearch", "Careers", "CORP"):
        assert do.label_from_site(site) is None, site


def test_a_token_is_ascii():
    assert do.slugify("Carrière - Coris Bank International") == "carriere-coris-bank-international"
    assert do.slugify("Texas Woman’s University | TWU") == "texas-womans-university-twu"


def test_a_pod_with_no_name_anywhere_falls_back_to_its_id_and_is_flagged():
    handler = _pod_handler({"__default__": (40, ["1"])}, title="Candidate Experience site")
    kind, row, _ = _check(handler, {"CX_1": 1})
    assert kind == "admitted" and row["label"] == row["token"] == "ebqb"
    assert do.unlabelled(row)
    branded = _check(_pod_handler({"__default__": (40, ["1"])}, title="Home Page"),
                     {"AperamCareerSite": 1})[1]
    assert branded["label"] == "Aperam" and not do.unlabelled(branded)


def test_the_label_prefers_og_site_name_over_the_title():
    page = ('<title>Job Search</title>'
            '<meta property="og:site_name" content="BDO USA Experienced Career Site"/>')
    assert do.read_site_page(page) == "BDO USA Experienced"
    assert do.read_site_page("<title>Careers at Pella</title>") == "Pella"


def test_sites_are_pruned_to_the_ones_reaching_new_jobs():
    # BDO, measured: three names answer the default site's 675 jobs; CX_1 is real.
    main = [str(i) for i in range(25)]
    checked = {
        "CX_xx": (675, main), "CX": (675, main), "BDOExperiencedCareers": (675, main),
        "CX_1": (618, main[:20] + ["campus1", "campus2"]),
        "jobsearch": (675, main), "dead": (0, []),
    }
    crawl = collections.Counter({"BDOExperiencedCareers": 7, "CX_xx": 3, "CX_1": 13, "CX": 1})
    # Ties at 675 go to the name the crawl uses most: the real site, not an alias.
    assert do.prune_sites(checked, crawl) == ["BDOExperiencedCareers", "CX_1"]


def test_tokens_are_unique_and_clear_of_other_boards():
    rows = [{"token": "deloitte", "host": "aaaa.fa.us2.oraclecloud.com"},
            {"token": "pella", "host": "bbbb.fa.us2.oraclecloud.com"},
            {"token": "pella", "host": "cccc.fa.us6.oraclecloud.com"}]
    do.assign_tokens(rows, taken={"lumen"}, slugs={"deloitte"})
    assert [r["token"] for r in rows] == ["deloitte-oracle", "pella", "pella-cccc"]


# --- The live check ---------------------------------------------------------------


def _pod_handler(sites: dict[str, tuple[int, list[str]]], title="Careers at Acme", seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        if "/hcmUI/" in request.url.path:
            return httpx.Response(200, text=f"<title>{title}</title>")
        finder = unquote(request.url.params["finder"])
        site = dict(p.split("=", 1) for p in finder.split(";", 1)[1].split(","))["siteNumber"]
        n, ids = sites.get(site, sites["__default__"])
        return httpx.Response(200, json={"items": [{"TotalJobsCount": n,
                                                    "requisitionList": [{"Id": i} for i in ids]}]})
    return handler


def _check(handler, sites):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await do.check_pod(client, POD, collections.Counter(sites), FAST_RETRY)
    return asyncio.run(go())


def test_a_pod_is_admitted_with_its_distinct_sites_and_a_label():
    seen: list = []
    handler = _pod_handler({"__default__": (675, ["1", "2"]), "CX_1": (618, ["2", "3"])},
                           title="BDO USA Experienced Career Site", seen=seen)
    kind, row, n = _check(handler, {"BDOExperiencedCareers": 7, "CX_xx": 3, "CX_1": 13})
    assert (kind, n) == ("admitted", 675)
    assert row == {"token": "bdo-usa-experienced", "label": "BDO USA Experienced",
                   "host": POD, "sites": ["BDOExperiencedCareers", "CX_1"]}
    # The check is the scraper's own list request, on the pod.
    assert all(r.url.host == POD for r in seen)


def test_a_pod_whose_sites_have_no_jobs_is_empty():
    kind, row, _ = _check(_pod_handler({"__default__": (0, [])}), {"CX_1": 1})
    assert (kind, row) == ("empty", None)


def test_a_404_pod_is_not_found_and_a_broken_one_unverified():
    assert _check(lambda r: httpx.Response(404), {"CX_1": 1})[0] == "not_found"
    assert _check(lambda r: httpx.Response(503), {"CX_1": 1})[0] == "unverified"
    assert _check(lambda r: httpx.Response(200, text="<html>"), {"CX_1": 1})[0] == "unverified"


def test_a_vanity_careers_url_is_read_for_its_pod():
    page = ('<link href="https://egug.fa.us2.oraclecloud.com/hcmRestApi/x">' * 3
            + '<img src="https://other.fa.us2.oraclecloud.com/y">')

    async def go():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text=page))
        async with httpx.AsyncClient(transport=transport) as client:
            return await do.vanity_to_pod(
                client, "https://careers.americanexpress.com/en/sites/CX_1/jobs/preview/1/", FAST_RETRY)

    assert asyncio.run(go()) == ("egug.fa.us2.oraclecloud.com", "CX_1")


# --- Merge ------------------------------------------------------------------------

_A = {"token": "alpha", "label": "Alpha", "host": "aaaa.fa.us2.oraclecloud.com", "sites": ["CX_1"]}
_Z = {"token": "zeta", "label": "Zeta", "host": "zzzz.fa.us2.oraclecloud.com", "sites": ["CX"]}


def test_merge_only_adds_by_host_and_keeps_the_file_format(tmp_path):
    path = tmp_path / "oracle_companies.json"
    path.write_bytes(json.dumps([_Z], indent=2).replace("\n", "\r\n").encode() + b"\r\n")
    assert do.merge(path, [_A, {**_Z, "sites": ["CX", "CX_9"]}]) == 1
    raw = path.read_bytes()
    assert json.loads(raw) == [_A, _Z]           # the shipped row is never edited
    assert raw.endswith(b"\r\n")


def test_nothing_in_the_tool_removes_an_employer():
    source = (paths.ROOT / "scripts" / "discover_oracle.py").read_text(encoding="utf-8")
    assert source.count("write_bytes(") == 1
    assert "[*existing, *fresh]" in source
    assert not re.search(r"\.(remove|discard|pop)\(", source)


def test_no_shipped_oracle_token_is_another_boards_slug():
    shipped = {r["token"] for r in json.loads(
        (paths.SHIPPED_CONFIG / "oracle_companies.json").read_text(encoding="utf-8"))}
    assert not shipped & do.de.other_board_slugs()
