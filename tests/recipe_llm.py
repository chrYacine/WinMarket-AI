"""Synthetic fact-search adapter used only by the explicitly selected browser recipe."""
class SyntheticFactClient:
    enabled = True
    last_provider_used = 'synthetic-lot56'

    def json_complete(self, prompt, system=None, temperature=None, max_tokens=None):
        return {'found':True,'passage_number':1,'value':5,'unit':'par semaine',
                'citation':'fr\u00e9quence de nettoyage standard est de 5 fois par semaine',
                'reason':'Synthetic captured response; citation is still verified by the real application'}


def install():
    import main  # Bind all normal disabled-LLM adapters before the fact-search-only injection.
    import src.agents.llm_client as module
    module.ClaudeClient = SyntheticFactClient
