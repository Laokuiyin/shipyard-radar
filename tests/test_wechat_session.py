from shipwatch.config import load_cookie_file
from shipwatch.fetch import Fetcher


def test_load_plain_wechat_cookie_file(tmp_path):
    cookie_file = tmp_path / "wechat_cookies.txt"
    cookie_file.write_text("wap_sid2=abc; appmsg_token=def", encoding="utf-8")

    assert load_cookie_file(cookie_file) == "wap_sid2=abc; appmsg_token=def"


def test_load_netscape_cookie_file(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".qq.com\tTRUE\t/\tTRUE\t0\tappmsg_token\tabc",
                ".example.com\tTRUE\t/\tTRUE\t0\tignored\tnope",
                "mp.weixin.qq.com\tFALSE\t/\tTRUE\t0\twap_sid2\tdef",
            ]
        ),
        encoding="utf-8",
    )

    assert load_cookie_file(cookie_file) == "appmsg_token=abc; wap_sid2=def"


def test_wechat_cookie_header_only_for_mp_domain():
    fetcher = Fetcher("Shipwatch/Test", wechat_cookie="wap_sid2=abc")

    assert fetcher._headers_for_url("https://mp.weixin.qq.com/s/test")["Cookie"] == "wap_sid2=abc"
    assert fetcher._headers_for_url("https://weixin.sogou.com/weixin") is None
    assert fetcher._headers_for_url("https://example.com") is None
