"""网页 LLM 通道(免 token)测试:工厂选择与 JSON 提取(不启动浏览器)。"""
from memvault.llm import get_llm_client, reset_llm_client
from memvault.llm_web import WebLLMClient


def test_factory_selects_backend():
    try:
        cfg = {"llm": {"backend": "web", "web": {}}}
        c = get_llm_client(cfg)
        assert isinstance(c, WebLLMClient)
        assert c.enabled is True
        assert c.model == "deepseek-web"

        cfg2 = {"llm": {"backend": "api", "api_key": "sk-x"}}
        c2 = get_llm_client(cfg2)
        assert type(c2).__name__ == "LLMClient"
        assert c2.api_key == "sk-x"
    finally:
        reset_llm_client()  # 清理缓存的 web 实例(不会启动过浏览器)


def test_web_client_extract_json_variants():
    assert WebLLMClient._extract_json('{"a": 1}') == {"a": 1}
    assert WebLLMClient._extract_json(
        '好的,结果如下:\n```json\n{"b": 2}\n```\n以上。') == {"b": 2}
    assert WebLLMClient._extract_json('说明{"c": 3}结尾') == {"c": 3}
    assert WebLLMClient._extract_json("没有 JSON 的回复") is None
    assert WebLLMClient._extract_json("") is None


def test_web_client_lazy_no_browser_on_init():
    """构造不应启动浏览器(仅首次真实调用才启动)。"""
    c = WebLLMClient({"llm": {"web": {}}})
    assert c.page is None and c.browser is None
    assert str(c.cookies_file).endswith("deepseek_web_auth.json")
