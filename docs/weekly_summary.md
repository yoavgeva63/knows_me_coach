# Weekly Summary — Feature Doc

## Overview

Every Saturday at 19:00 Israel time, the bot sends a Weekly Review covering the
current Sun–Sat week. Users can also trigger it manually with `/weekly` or by
asking the coach in natural language ("send me the week summary").

---

## Triggers

| Trigger | How |
|---|---|
| Auto, Saturday 19:00 | `trigger_briefings.py` → `_check_and_send_weekly()` |
| `/weekly` command | `bot.py weekly()` → `weekly_briefing.send_weekly_briefing()` |
| Natural language | `get_claude_response` calls `trigger_weekly_briefing` tool → `bot.py` tool loop |

`mark_weekly_sent(user_id, saturday_str)` is called on success so `trigger_briefings.py`
never double-sends within the same Saturday.

---

## Week Boundary

Always **Sunday through Saturday** of the current week. Computed by
`weekly_briefing._get_week_bounds()` using **Israel time** (UTC+2):

```python
now_israel = datetime.now(timezone.utc) + timedelta(hours=_ISRAEL_UTC_OFFSET_H)
today = now_israel.date()
days_since_sunday = (today.weekday() + 1) % 7
sunday = today - timedelta(days=days_since_sunday)
saturday = sunday + timedelta(days=6)
```

When `/weekly` is triggered mid-week, data is shown for Sun through yesterday
(entries simply won't exist yet for future days).

---

## Message Format

```
📊 Weekly Review — Mar 9–15

🏋️ Training  (3/4 gym · 1/2 runs)
Mon: ✅ Upper body — chest & triceps
Wed: ✅ Lower body — heavy squat day
Thu: ✏️ 5km run (instead)
Sat: ❌ Skipped
─ Missed: Fri

🥗 Nutrition  (5/7 days logged)
Avg 2,340 kcal · target 2,500
Avg 162g protein · target 180g

😴 Recovery
Avg sleep 7h 20min · score 74/100  (6/7 nights)
HRV stable around baseline
Total steps 68,400

vs last week: +1 session 📈

💬 Coach's Take
<Claude insight paragraph>
```

**Nutrition low-data fallback** (< 3 days logged):
```
🥗 Nutrition
Not enough data this week — log your meals daily to unlock nutrition insights.
```

**No Garmin:** Recovery section is omitted entirely.
**No last-week data:** Comparison line is omitted.
**Partial sleep data:** Recovery shows `(N/7 nights)` when fewer than 7 nights have data.

---

## Status Emojis

| Emoji | Meaning |
|---|---|
| ✅ | Done (explicit or assumed — see below) |
| ✏️ | Modified (different from recommended) |
| ❌ | Skipped |

**Assume-done:** Entries in `workout_history` with no `status` field (i.e. the user never
tapped Done/Skip/Modify and never mentioned the workout in chat) are displayed as ✅ and
counted toward gym/run totals. The weekly summary errs on the side of trusting the user
completed the workout.

---

## Data Sources

| Section | Source |
|---|---|
| Training day-by-day | `profile.workout_history` filtered to Sun–Sat |
| Gym / run counts | `_is_cardio()` keyword heuristic on `summary` / `actual_summary` |
| Nutrition averages | `profile.nutrition.daily_meals` filtered to Sun–Sat |
| Recovery | `garmin.fetch_week_stats(user_id, sunday, saturday)` — ~15 live API calls (7 sleep + 7 steps + 1 HRV); skipped if no Garmin connected |
| Last-week comparison | `workout_history` entries from previous Sun–Sat; counts entries where `status != "skipped"` |
| Coach's Take context | Training block + nutrition block + this week's chat (filtered by `ts`) + `coach_notes` |

**Garmin fetch optimisation:** `fetch_week_stats` only requests days up to `date.today()` —
future dates are skipped mid-week. HRV uses `min(saturday, date.today())` to avoid fetching
a future date's endpoint.

---

## Cardio Detection

`_is_cardio(summary)` determines whether a session counts as a run or gym session:

```python
_CARDIO_WORDS = {"run", "runs", "running", "jog", "jogging", "sprint", "sprinting", "pace"}

def _is_cardio(summary: str) -> bool:
    low = summary.lower()
    return bool(set(low.split()) & _CARDIO_WORDS) or "km" in low
```

Word-level matching (split on whitespace) prevents false positives from compound phrases
like "burn fat — chest day". `"km"` retains a substring check since it appears fused
("5km", "10km").

---

## Workout Completion Logging

Workout entries in `workout_history` start with no `status` field (just `date` + `summary`).
Status is added later via two paths:

### Button path (ConversationHandler in `bot.py`)

When the user taps **💪 Workout** on the morning briefing, the full workout text is
shown with three inline buttons:

```
[ ✅ Done ]  [ ✏️ Modify ]  [ ❌ Skip ]
```

The callback data embeds the **Israel-time date** at button-creation time:
`workout:done:2026-03-14`, `workout:modify:2026-03-14`, `workout:skip:2026-03-14`.

This means tapping an old button the next day still logs against the correct date.

- **Done / Skip** → immediately call `storage.update_workout_status()` and END.
  Returns `bool` — if `False` (entry rotated out), sends an error message instead of "✅ Logged!".
- **Modify** → bot asks *"What's your plan? Tell me what you'll change or what you'll do instead."*
  The next message the user sends is treated as the modification description (no `/cancel` needed).
  → `brain.interpret_workout_modification()` interprets the text →
  `storage.update_workout_status()` with `status="modified"`, `actual_summary`, `actual_type`.

### Natural language path (ACTION_TOOL)

When the user mentions workout completion in chat (e.g. "I changed my workout a bit"),
Claude calls the `log_workout_status` tool directly with `status`, `actual_summary`,
and `actual_type` already inferred from context. No second Claude call needed.

**Tool restriction:** The `log_workout_status` tool description explicitly instructs Claude
to only call it for **today's** workout. Past-day mentions are acknowledged conversationally
but do not trigger the tool.

### Storage schema per entry

```json
{
  "date": "2026-03-14",
  "summary": "Upper body — chest & triceps (recommended)",
  "status": "done | modified | skipped",
  "actual_summary": "Upper body, 3 sets, skipped lunges",
  "actual_type": "same_modified | different"
}
```

`actual_summary` and `actual_type` are only present when `status == "modified"`.
Re-logging is allowed — the status is always overwritten.

---

## Key Files

| File | Role |
|---|---|
| `weekly_briefing.py` | Orchestrator — gathers data, builds sections, calls Claude, sends message |
| `brain/weekly_summary.py` | Claude call — returns `coaches_take` string |
| `brain/workout_log.py` | Claude call — interprets modify description (button path only) |
| `storage.py` | `update_workout_status` (returns `bool`), `mark_weekly_sent`, `get_weekly_sent_saturday` |
| `trigger_briefings.py` | Auto-trigger on Saturday ≥ 19:00 |
| `bot.py` | ConversationHandler, `/weekly` command, tool loop handlers |
