"""工商档案: the public dossier looks for the registered address. Synthetic, no network."""


def test_public_dossier_looks_for_the_registered_address():
    from project_watch.studio import public_profile as pp

    class Collect:
        has_search = True

        def __init__(self):
            self.queries = []

        def web_search(self, query, *, count=10, **kw):
            self.queries.append(query)
            return []

    class Model:
        def json(self, *a, **kw):
            return {"facts": []}

    collectors = Collect()
    pp.build_public_profile({"name": "光启视界", "legal_name": "光启视界科技有限公司"}, collectors, Model(), [])
    assert "光启视界科技有限公司 注册地址 统一社会信用代码" in collectors.queries
    assert "label=注册地址" in pp.SYSTEM and "总部、办公地或研发基地不是注册地址" in pp.SYSTEM
