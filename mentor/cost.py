"""Token cost logging and a monthly budget guard. Prices are USD per million tokens
(from the Claude API model table, 2026-09-25). Cache write is assumed at 1.25x input (5-minute TTL)."""
PRICES = {  # model: (input, output, cache_read)
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
MONTHLY_BUDGET_USD = 10.00
WARN_AT = 0.80


def cost_of(model, input_tokens, output_tokens, cache_read=0, cache_write=0):
    pin, pout, pread = PRICES[model]
    return (input_tokens * pin + output_tokens * pout + cache_read * pread + cache_write * pin * 1.25) / 1e6


def log_usage(conn, model, usage):
    """usage: an Anthropic response.usage (or anything with the same attributes)."""
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    c = cost_of(model, inp, out, cr, cw)
    conn.execute(
        "INSERT INTO usage(at, model, input_tokens, output_tokens, cache_read, cache_write, cost_usd) "
        "VALUES (datetime('now','localtime'),?,?,?,?,?,?)", (model, inp, out, cr, cw, c))
    conn.commit()
    return c


def month_spend(conn):
    row = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM usage "
                       "WHERE strftime('%Y-%m', at) = strftime('%Y-%m', 'now', 'localtime')").fetchone()
    return row[0]


def check_budget(conn, budget=MONTHLY_BUDGET_USD):
    """Returns (ok, message). ok=False means do not make another API call."""
    spent = month_spend(conn)
    if spent >= budget:
        return False, f"Monthly budget reached: ${spent:.2f} of ${budget:.2f}. No more API calls this month."
    if spent >= WARN_AT * budget:
        return True, f"Heads up: ${spent:.2f} of ${budget:.2f} monthly budget used."
    return True, ""
