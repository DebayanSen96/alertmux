from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from alertmux.query import AlertsResponse
from alertmux.registry import (
    URL,
    RegisterUnavailable,
    alpha3_to_alpha2,
    build_authorities_response,
    clear_cache,
    get_register,
    parse,
)
from alertmux.schema import NormalisedAlert, Provenance

FIXTURE = (Path(__file__).parent / "fixtures" / "wmo_register.xml").read_bytes()
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def teardown_function():
    clear_cache()


def _alert(authority: str, source_id: str = "wmo-swic"):
    return NormalisedAlert(
        id=f"{source_id}:{authority}:1",
        event="THUNDERSTORMS",
        provenance=Provenance(
            authority=authority,
            source_id=source_id,
            source_url="https://example.invalid",
            retrieved_at=NOW,
        ),
        unavailable_fields=[],
    )


def _alerts_response(authorities: list[str]) -> AlertsResponse:
    return AlertsResponse(
        alerts=[_alert(a) for a in authorities],
        sources=[],
        partial=False,
        retrieved_at=NOW,
    )


# --- parsing the fixture -----------------------------------------------


def test_parse_returns_every_item():
    authorities = parse(FIXTURE)
    assert len(authorities) == 7


def test_nigeria_entry_parses_country_and_authority_names():
    authorities = parse(FIXTURE)
    nigeria = next(a for a in authorities if a.alpha3 == "NGA")
    assert nigeria.country_name == "Nigeria"
    assert nigeria.authority_name == "Nigerian Meteorological Agency"
    assert nigeria.alpha2 == "ng"
    assert nigeria.abbrev == "nma"


def test_usa_noaa_entry_parses():
    authorities = parse(FIXTURE)
    noaa = next(
        a for a in authorities if a.alpha3 == "USA" and a.abbrev == "noaa"
    )
    assert noaa.alpha2 == "us"
    assert noaa.country_name == "United States of America"


def test_entry_lacking_authority_abbrev_stays_none_not_guessed():
    authorities = parse(FIXTURE)
    cmo = next(
        a
        for a in authorities
        if a.alpha3 == "VCT" and a.authority_name == "Caribbean Meteorological Organization"
    )
    assert cmo.abbrev is None


def test_unmappable_alpha3_still_parses_with_no_alpha2():
    """EUM (EUMETNET) is not a country and has no ISO 3166-1 alpha-2 --
    it must still appear in the parsed list, just with alpha2=None."""
    authorities = parse(FIXTURE)
    eumetnet = next(a for a in authorities if a.alpha3 == "EUM")
    assert eumetnet.alpha2 is None


def test_non_rss_envelope_raises():
    with pytest.raises(ValueError, match="rss"):
        parse(b"<html><body>not a feed</body></html>")


def test_unparseable_xml_raises():
    with pytest.raises(ValueError, match="not parseable XML"):
        parse(b"<rss><channel><item>")


# --- alpha-3 -> alpha-2 table --------------------------------------------


@pytest.mark.parametrize(
    "alpha3,alpha2",
    [
        ("NGA", "ng"),
        ("USA", "us"),
        ("DEU", "de"),
        ("GBR", "gb"),
        ("ZAF", "za"),
    ],
)
def test_alpha3_to_alpha2_known_codes(alpha3, alpha2):
    assert alpha3_to_alpha2(alpha3) == alpha2


def test_alpha3_to_alpha2_unmappable_code_returns_none():
    assert alpha3_to_alpha2("EUM") is None


def test_alpha3_to_alpha2_none_input_returns_none():
    assert alpha3_to_alpha2(None) is None


# --- coverage / matching --------------------------------------------------


def test_countries_covered_reflects_alerts_actually_present():
    authorities = parse(FIXTURE)
    response = build_authorities_response(
        authorities, NOW, 0.0, None, _alerts_response(["ng-nimet", "us-noaa"])
    )
    assert "ng" in response.countries_covered
    assert "us" in response.countries_covered
    assert "de" in response.countries_uncovered
    assert "za" in response.countries_uncovered
    assert "gb" in response.countries_uncovered


def test_countries_uncovered_is_the_complement_of_covered():
    authorities = parse(FIXTURE)
    response = build_authorities_response(
        authorities, NOW, 0.0, None, _alerts_response(["ng-nimet"])
    )
    register_alpha2 = {a.alpha2 for a in authorities if a.alpha2}
    assert set(response.countries_covered) | set(response.countries_uncovered) == register_alpha2
    assert set(response.countries_covered) & set(response.countries_uncovered) == set()


def test_unmatched_authority_is_not_reported_as_matched():
    """Nigeria's WMO abbrev (nma) disagrees with our own authority slug
    (ng-nimet) -- the entry must stay unmatched even though the
    country is covered by an alert from ng-nimet this fetch."""
    authorities = parse(FIXTURE)
    response = build_authorities_response(
        authorities, NOW, 0.0, None, _alerts_response(["ng-nimet"])
    )
    nigeria = next(a for a in response.authorities if a.alpha3 == "NGA")
    assert nigeria.matched_authority is None
    # And yet the country itself is covered -- unmatched != uncovered.
    assert "ng" in response.countries_covered


def test_exact_abbrev_reconstruction_does_match():
    """us-noaa is the one case (measured live) where WMO's own abbrev
    equals our authority slug's suffix."""
    authorities = parse(FIXTURE)
    response = build_authorities_response(
        authorities, NOW, 0.0, None, _alerts_response(["us-noaa"])
    )
    noaa = next(
        a for a in response.authorities if a.alpha3 == "USA" and a.abbrev == "noaa"
    )
    assert noaa.matched_authority == "us-noaa"


def test_unmappable_country_codes_surfaces_eumetnet():
    authorities = parse(FIXTURE)
    response = build_authorities_response(
        authorities, NOW, 0.0, None, _alerts_response([])
    )
    assert "EUM" in response.unmappable_country_codes


def test_country_filter_accepts_alpha2():
    authorities = parse(FIXTURE)
    response = build_authorities_response(
        authorities, NOW, 0.0, None, _alerts_response([]), country="ng"
    )
    assert len(response.authorities) == 1
    assert response.authorities[0].alpha3 == "NGA"


def test_country_filter_accepts_alpha3():
    authorities = parse(FIXTURE)
    response = build_authorities_response(
        authorities, NOW, 0.0, None, _alerts_response([]), country="NGA"
    )
    assert len(response.authorities) == 1
    assert response.authorities[0].alpha3 == "NGA"


def test_totals_reflect_the_whole_register_not_the_filter():
    authorities = parse(FIXTURE)
    response = build_authorities_response(
        authorities, NOW, 0.0, None, _alerts_response([]), country="NGA"
    )
    assert response.total_authorities == 7
    assert response.total_countries == 7


def test_response_carries_the_disclaimer():
    authorities = parse(FIXTURE)
    response = build_authorities_response(
        authorities, NOW, 0.0, None, _alerts_response([])
    )
    assert "official alerting authorities" in response.disclaimer


# --- caching / fetch failure ----------------------------------------------


@respx.mock
def test_get_register_fetches_and_caches():
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=FIXTURE))
    authorities, fetched_at, age, error = get_register()
    assert len(authorities) == 7
    assert error is None
    assert age == 0.0

    # A second call within the TTL must not refetch.
    get_register()
    assert route.call_count == 1


@respx.mock
def test_fetch_failure_with_no_cache_raises_register_unavailable():
    respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(RegisterUnavailable):
        get_register()


@respx.mock
def test_fetch_failure_with_existing_cache_serves_stale_copy_and_reports_age():
    respx.get(URL).mock(return_value=httpx.Response(200, content=FIXTURE))
    authorities, _, _, error = get_register()
    assert error is None
    assert len(authorities) == 7

    clear_cache_route = respx.get(URL).mock(return_value=httpx.Response(503))
    # Force the TTL to have "expired" by clearing the monotonic gate --
    # simplest is to reach in and drop the cache's freshness by
    # monkeypatching CACHE_TTL_SECONDS via the module, but a subtler
    # and more honest approach is to directly poke the cache internals
    # via clear_cache()+seed. Since get_register() only re-fetches past
    # the TTL, and the TTL is 24h, simulate expiry by clearing the
    # cache attribute directly is out of scope for this module's public
    # surface -- instead, verify the *documented contract* by forcing a
    # failure on a register with no prior cache, which is covered above,
    # and by directly exercising the fallback branch through the cache
    # object.
    import alertmux.registry as registry_module

    cached = registry_module._cache
    assert cached is not None
    cached.fetched_monotonic -= registry_module.CACHE_TTL_SECONDS + 1

    authorities2, fetched_at2, age2, error2 = get_register()
    assert len(authorities2) == 7
    assert error2 is not None
    assert age2 > 0
    assert clear_cache_route.called


# --- HTTP endpoint ----------------------------------------------------


def _endpoint_client(alerts_authorities):
    from fastapi.testclient import TestClient

    from alertmux.api import app, get_adapters
    from alertmux.adapters.base import FetchResult

    class FakeAdapter:
        source_id = "wmo-swic"

        def fetch(self):
            return FetchResult(
                source_id="wmo-swic",
                ok=True,
                alerts=[_alert(a) for a in alerts_authorities],
                retrieved_at=NOW,
                latency_ms=1,
            )

    app.dependency_overrides[get_adapters] = lambda: [FakeAdapter()]
    return TestClient(app)


def _endpoint_teardown():
    from alertmux.api import app, clear_cache as clear_api_cache

    app.dependency_overrides.clear()
    clear_api_cache()
    clear_cache()


@respx.mock
def test_authorities_endpoint_returns_register_and_coverage():
    respx.get(URL).mock(return_value=httpx.Response(200, content=FIXTURE))
    client = _endpoint_client(["ng-nimet"])
    try:
        response = client.get("/authorities")
        assert response.status_code == 200
        body = response.json()
        assert body["total_authorities"] == 7
        assert "ng" in body["countries_covered"]
        assert "de" in body["countries_uncovered"]
        assert "EUM" in body["unmappable_country_codes"]
        assert "disclaimer" in body
    finally:
        _endpoint_teardown()


@respx.mock
def test_authorities_endpoint_filters_by_country():
    respx.get(URL).mock(return_value=httpx.Response(200, content=FIXTURE))
    client = _endpoint_client([])
    try:
        response = client.get("/authorities", params={"country": "NGA"})
        body = response.json()
        assert len(body["authorities"]) == 1
        assert body["authorities"][0]["alpha3"] == "NGA"
    finally:
        _endpoint_teardown()


@respx.mock
def test_authorities_endpoint_returns_503_with_no_cache_and_failed_fetch():
    respx.get(URL).mock(return_value=httpx.Response(503))
    client = _endpoint_client([])
    try:
        response = client.get("/authorities")
        assert response.status_code == 503
        assert "error" in response.json()
    finally:
        _endpoint_teardown()
