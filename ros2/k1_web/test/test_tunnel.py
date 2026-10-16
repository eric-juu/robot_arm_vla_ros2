from k1_web.tunnel import find_public_url


def test_finds_quick_tunnel_url_in_cloudflared_log():
    line = "2026-10-16T10:00:00Z INF |  https://brave-otter-river-lamp.trycloudflare.com                    |"
    assert find_public_url(line) == "https://brave-otter-river-lamp.trycloudflare.com"


def test_ignores_other_cloudflare_urls():
    assert find_public_url("INF Requesting new quick Tunnel on https://api.trycloudflare.com...") is None
    assert find_public_url("INF Version 2026.9.0") is None
