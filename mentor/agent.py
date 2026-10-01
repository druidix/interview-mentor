"""Interview Mentor agent: a manual tool-use loop on the raw Anthropic SDK.

Models: Sonnet 5.5 plays interviewer/debriefer; Haiku 4.5 writes quiz questions (cheaper).
"""
import json
import random

import anthropic

from . import cost, db

INTERVIEWER = "claude-sonnet-5-5"
QUIZ_MODEL = "claude-haiku-4-5"
MAX_FOLLOWUPS = 3

SYSTEM = f"""You are Interview Mentor for Kaushik, a senior technology leader preparing for CTO and SVP/VP Engineering interviews.
Style: encouraging and direct, a few emojis, small celebrations when he improves. Never flatter; correct errors plainly.

Run each session in this order:
1. Call get_due_items. If any items are due, quiz him on those first (call generate_quiz for each), then record each result with record_quiz_result.
2. Call get_scenario (use the category he asks for, otherwise none). Play the interviewer: state the scenario cold in 3-5 sentences. If the scenario has 'my_answer' or 'notes', treat them as his earlier attempt and probe where it was weak. Do not reveal them.
3. After his answer, ask at most {MAX_FOLLOWUPS} follow-up questions in total, one at a time, pushing on assumptions and trade-offs.
4. Debrief: score 1-5 on each rubric dimension below, list strengths, gaps and corrections, then call save_assessment.
5. For every real gap, call add_gap_item (short topic, one-sentence correction), then offer a quick quiz on the new material.

Rubric dimensions by category:
- architecture: requirements_clarification, component_choices, failure_handling, scale_latency, tradeoffs
- leadership: situation_framing, decision_quality, people_impact, communication, ownership
- exec-communication: clarity, business_relevance, brevity, evidence, call_to_action

Keep replies concise. Do not invent facts about his history beyond what the scenario data says."""

TOOLS = [
    {"name": "get_due_items", "description": "List gap items due for re-quiz today (max 3).",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_scenario", "description": "Get the next interview scenario (least recently asked).",
     "input_schema": {"type": "object", "properties": {
         "category": {"type": "string", "enum": ["architecture", "leadership", "exec-communication"]}},
         "additionalProperties": False}},
    {"name": "generate_quiz", "description": "Write a multiple-choice question for a gap item. Options come back already shuffled.",
     "input_schema": {"type": "object", "properties": {
         "topic": {"type": "string"}, "correction": {"type": "string"}},
         "required": ["topic", "correction"], "additionalProperties": False}},
    {"name": "record_quiz_result", "description": "Record whether Kaushik answered a gap item's quiz correctly.",
     "input_schema": {"type": "object", "properties": {
         "gap_id": {"type": "integer"}, "correct": {"type": "boolean"}},
         "required": ["gap_id", "correct"], "additionalProperties": False}},
    {"name": "save_assessment", "description": "Save the 1-5 rubric scorecard for this session.",
     "input_schema": {"type": "object", "properties": {
         "scenario_id": {"type": "integer"},
         "scores": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 1, "maximum": 5}},
         "strengths": {"type": "array", "items": {"type": "string"}},
         "gaps": {"type": "array", "items": {"type": "string"}}},
         "required": ["scenario_id", "scores", "strengths", "gaps"], "additionalProperties": False}},
    {"name": "add_gap_item", "description": "Add or reset a gap item to be re-quizzed (due tomorrow).",
     "input_schema": {"type": "object", "properties": {
         "topic": {"type": "string"}, "correction": {"type": "string"}},
         "required": ["topic", "correction"], "additionalProperties": False}},
]


def generate_quiz(client, conn, topic, correction):
    """Haiku writes the question; code shuffles the options so position never leaks the answer."""
    ok, msg = cost.check_budget(conn)
    if not ok:
        return {"error": msg}
    resp = client.messages.create(
        model=QUIZ_MODEL, max_tokens=500,
        system="Write one multiple-choice question testing the given correction. Make all four options similar in length and specificity; the three wrong options must be plausible common misconceptions, not absurd; do not leak the answer in the question stem. Reply with ONLY JSON: "
               '{"question": str, "correct": str, "wrong": [str, str, str]}',
        messages=[{"role": "user", "content": f"Topic: {topic}\nCorrect understanding: {correction}"}])
    cost.log_usage(conn, QUIZ_MODEL, resp.usage)
    text = next(b.text for b in resp.content if b.type == "text")
    try:
        q = json.loads(text[text.index("{"): text.rindex("}") + 1])
        options = [q["correct"]] + list(q["wrong"])[:3]
    except (ValueError, KeyError):
        return {"error": "quiz generation returned invalid JSON; ask a free-form question instead"}
    random.shuffle(options)
    return {"question": q["question"], "options": options, "answer": q["correct"]}


def run_tool(client, conn, name, args):
    if name == "get_due_items":
        return db.due_items(conn)
    if name == "get_scenario":
        return db.pick_scenario(conn, args.get("category")) or {"error": "no scenarios; run the importer"}
    if name == "generate_quiz":
        return generate_quiz(client, conn, args["topic"], args["correction"])
    if name == "record_quiz_result":
        try:
            return {"new_interval_days": db.record_quiz(conn, args["gap_id"], args["correct"])}
        except ValueError as e:
            return {"error": str(e)}
    if name == "save_assessment":
        scorecard = {k: args[k] for k in ("scores", "strengths", "gaps")}
        return {"session_id": db.save_session(conn, args["scenario_id"], scorecard)}
    if name == "add_gap_item":
        db.add_gap(conn, args["topic"], args["correction"])
        return {"ok": True}
    return {"error": f"unknown tool {name}"}


def agent_turn(client, conn, messages):
    """Run the model until it produces a final text answer (end_turn). Returns the text."""
    while True:
        ok, msg = cost.check_budget(conn)
        if not ok:
            return msg
        if msg:
            print(f"[{msg}]")
        resp = client.messages.create(
            model=INTERVIEWER, max_tokens=2000, system=SYSTEM, tools=TOOLS, messages=messages,
            cache_control={"type": "ephemeral"}, output_config={"effort": "medium"})
        cost.log_usage(conn, INTERVIEWER, resp.usage)
        messages.append({"role": "assistant", "content": resp.content})  # keep ALL blocks, incl. thinking
        if resp.stop_reason == "refusal":
            return "(The model declined this request.)"
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                out = run_tool(client, conn, b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out), "is_error": isinstance(out, dict) and "error" in out})
        messages.append({"role": "user", "content": results})  # all results in ONE message


def main():
    conn = db.connect()
    db.seed_gaps(conn)
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": "Let's start a session."}]
    print(f"Interview Mentor. Type /quit to end. Spent this month: ${cost.month_spend(conn):.2f} of ${cost.MONTHLY_BUDGET_USD:.2f}\n")
    while True:
        print(agent_turn(client, conn, messages), "\n")
        user = input("you> ").strip()
        if user in ("/quit", "/q"):
            break
        messages.append({"role": "user", "content": user})
    print(f"Session cost so far this month: ${cost.month_spend(conn):.2f}")
