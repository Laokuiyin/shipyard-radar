from shipwatch.parsers import extract_web_article, extract_wechat_article
from shipwatch.collector import Collector
from shipwatch.text import normalize_url


def test_extract_web_article():
    html = """
    <html><head><meta property="og:title" content="某船开工">
    <meta property="article:published_time" content="2026-05-01"></head>
    <body><article>沪东中华为某船东建造2艘174000立方米LNG船，首制船正式开工。</article></body>
    </html>
    """
    title, content, published = extract_web_article(html, "https://example.com/a")
    assert title == "某船开工"
    assert "正式开工" in content
    assert published.isoformat() == "2026-05-01"


def test_extract_wechat_article():
    html = """
    <html><body><h1 id="activity-name">新船交付</h1>
    <strong id="js_name">沪东中华造船</strong>
    <div id="js_content">公司为某船东建造的LNG船正式交付。</div>
    <script>var ct = "1760000000";</script></body></html>
    """
    title, content, published, account = extract_wechat_article(html)
    assert title == "新船交付"
    assert "正式交付" in content
    assert published is not None
    assert account == "沪东中华造船"


def test_resolve_sogou_script_target():
    html = """
    <script>
      var url = '';
      url += 'https://mp.';
      url += 'weixin.qq.com/s/abc';
      window.location.replace(url);
    </script>
    """
    assert Collector._sogou_target(html) == "https://mp.weixin.qq.com/s/abc"


def test_normalize_wechat_captcha_url_to_target_article():
    url = (
        "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=abc"
        "&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2FgZULykpwdtdBLiorj5u-uQ"
    )

    assert normalize_url(url) == "https://mp.weixin.qq.com/s/gZULykpwdtdBLiorj5u-uQ"
