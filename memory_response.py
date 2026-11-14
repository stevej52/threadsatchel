"""Budget the auxiliary read tools without silently changing source evidence."""
from memory_budget import lookup_budget


def bounded_auxiliary(root, result, budget_tokens=2000):
    with lookup_budget(root, budget_tokens) as budget:
        if budget.fits(result):
            return budget.finish(result)
        return budget.finish({
            'status': 'response_too_large',
            'content_omitted': True,
            'message': 'This auxiliary result exceeds the retrieval budget. '
                       'Use search_memory with a specific topic, then retrieve '
                       'a source block or range. No source text was truncated.'})
