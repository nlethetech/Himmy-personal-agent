"""Finance connector — let Himmy LOG and ANSWER about the user's spending in chat.

Thin tool layer over :class:`himmy_app.finance.ExpenseStore`:
  * ``add_expense``     — "log Rs 500 for groceries", "I spent 1200 on fuel yesterday";
  * ``list_expenses``   — "what did I spend this week", "show my food expenses";
  * ``expense_summary`` — "how much have I spent this month", "where's my money going".

Snapping a bill (photo → structured expense) is the app's ``/finance/snap`` endpoint (it needs the
image bytes); these tools cover everything the agent can do from text. Read-only tools are marked
so; ``add_expense`` writes but is low-risk (a ledger entry the user can delete), so it is NOT
approval-gated.
"""

from __future__ import annotations

from typing import Any

from himmy.services.tools.registry import ToolRegistry

from himmy_app.config import HimmyConfig, load_config
from himmy_app.connectors._register import safe_register_local_tool
from himmy_app.finance import CATEGORIES, ExpenseStore


class FinanceConnector:
    """Registers add_expense / list_expenses / expense_summary over the expense ledger."""

    def __init__(self, config: HimmyConfig | None = None) -> None:
        self._cfg = config or load_config()

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        cfg = self._cfg

        async def add_expense(args: dict[str, Any]) -> dict[str, Any]:
            amount = args.get("amount")
            if not amount:
                return {"ok": False, "message": "How much was it? Give me an amount."}
            exp = ExpenseStore(cfg).add({
                "amount": amount, "merchant": args.get("merchant") or args.get("note") or "Expense",
                "category": args.get("category"), "date": args.get("date"),
                "note": args.get("note"), "currency": args.get("currency"),
            }, source="chat")
            return {"ok": True, "expense": exp,
                    "message": f"Logged {exp['currency']} {exp['amount']:.0f} for "
                               f"{exp['merchant']} ({exp['category']})."}

        async def list_expenses(args: dict[str, Any]) -> dict[str, Any]:
            store = ExpenseStore(cfg)
            period = str(args.get("period") or "month")
            frm, to, _ = store._range_for(period)
            month = frm[:7] if (frm and to and frm[:7] == to[:7] and period in ("month", "this_month")) else None
            rows = store.list(limit=int(args.get("limit") or 40),
                              month=month, category=args.get("category"))
            # When the period isn't a single month, filter the (date-sorted) rows to the range.
            if frm and not month:
                rows = [r for r in rows if (r.get("date") or "") >= frm]
            total = round(sum(float(r.get("amount") or 0) for r in rows), 2)
            return {"ok": True, "period": period, "count": len(rows),
                    "total": total, "expenses": rows}

        async def expense_summary(args: dict[str, Any]) -> dict[str, Any]:
            return ExpenseStore(cfg).summary(str(args.get("period") or "month"))

        async def scan_email_spending(args: dict[str, Any]) -> dict[str, Any]:
            from himmy_app.email_money import scan_email_for_spending

            return await scan_email_for_spending(
                cfg, lookback=int(args.get("lookback") or 40),
                add=bool(args.get("add", True)),
            )

        names: list[str] = []
        cats = ", ".join(CATEGORIES)
        n = safe_register_local_tool(
            registry, name="add_expense", read_only=False, handler=add_expense,
            description=(
                "Log a spending EXPENSE to the user's finance ledger when they tell you they spent "
                "something ('log Rs 500 for groceries', 'I paid 1200 for fuel yesterday'). Pass "
                "`amount` (NPR number, required) and, when known, `merchant`, `category` (one of: "
                f"{cats}), `date` (YYYY-MM-DD — resolve 'yesterday'/'today' with current_time), and "
                "`note`. It just records the expense (the user can delete it); confirm what you "
                "logged. To READ spending use expense_summary / list_expenses instead."
            ),
            args_json_schema={"type": "object", "properties": {
                "amount": {"type": "number"}, "merchant": {"type": "string"},
                "category": {"type": "string", "enum": CATEGORIES},
                "date": {"type": "string"}, "note": {"type": "string"},
                "currency": {"type": "string"}}, "required": ["amount"]},
        )
        if n:
            names.append(n)
        n = safe_register_local_tool(
            registry, name="expense_summary", read_only=True, handler=expense_summary,
            description=(
                "Summarise the user's spending — total + a breakdown BY CATEGORY — for a period. "
                "Use for 'how much have I spent this month', 'where's my money going', 'am I "
                "overspending'. Optional `period`: 'month' (default, this calendar month), 'week' "
                "(last 7 days), 'year', or 'all'. Returns total, count, currency, and by_category. "
                "Lead with the total, then the top categories; if they have a Food budget in their "
                "vault, compare their Food spend to it."
            ),
            args_json_schema={"type": "object", "properties": {
                "period": {"type": "string", "enum": ["week", "month", "year", "all"]}}},
        )
        if n:
            names.append(n)
        n = safe_register_local_tool(
            registry, name="list_expenses", read_only=True, handler=list_expenses,
            description=(
                "List the user's recent EXPENSES (each with date, merchant, amount, category). Use "
                "for 'what did I spend this week', 'show my food expenses', 'my recent spending'. "
                "Optional `period` ('week'|'month'|'year'|'all'), `category` (e.g. 'Food'), and "
                "`limit`. Returns the matching expenses and their total."
            ),
            args_json_schema={"type": "object", "properties": {
                "period": {"type": "string"}, "category": {"type": "string"},
                "limit": {"type": "integer"}}},
        )
        if n:
            names.append(n)
        n = safe_register_local_tool(
            registry, name="scan_email_spending", read_only=False, handler=scan_email_spending,
            description=(
                "Scan the user's recent Gmail for real SPENDING (receipts, order/payment "
                "confirmations, charges, bills, subscriptions) and LOG each one to their finance "
                "ledger. Use when they ask you to 'find my spending from email', 'check my email for "
                "purchases', 'update my expenses from my inbox', or to keep their ledger current. It "
                "skips promos, OTPs, shipping pings and refunds, and never logs the same email twice. "
                "Optional `lookback` (how many recent emails to read, default 40) and `add` (set "
                "false to preview without saving). Report how many it found and the total."
            ),
            args_json_schema={"type": "object", "properties": {
                "lookback": {"type": "integer"}, "add": {"type": "boolean"}}},
        )
        if n:
            names.append(n)
        return names


__all__ = ["FinanceConnector"]
