"""
Rule 206(4)-1 "Marketing Rule" disclosure text.

Any hypothetical performance output (backtest, projection) delivered to a
prospective or current client must carry a disclosure that it is hypothetical,
identify material conditions and limitations, and state the risks. These
strings are bundled into backtest responses and agent summaries so the
disclosure travels with the data.

Wording reviewed for plain-English intent; NOT lawyer-reviewed. Swap in
counsel-approved text before launch.
"""

BACKTEST_DISCLOSURE = (
    "IMPORTANT: Results shown are HYPOTHETICAL and reconstructed from real "
    "historical adjusted-close prices (dividends reinvested, splits adjusted). "
    "Hypothetical results have inherent limitations: they do not reflect actual "
    "trading, are prepared with the benefit of hindsight, and cannot account for "
    "all factors that would affect real investment decisions. No representation "
    "is being made that any account will or is likely to achieve profits or "
    "losses similar to those shown. Past performance is not indicative of future "
    "results. Fees, taxes, and slippage may reduce actual returns. Required by "
    "SEC Rule 206(4)-1."
)

ADVISOR_DISCLOSURE = (
    "This analysis is for informational purposes only and does not constitute "
    "investment, tax, or legal advice. Tax-loss harvesting outcomes depend on your "
    "complete tax picture including outside accounts. Consult your tax advisor "
    "before acting on these recommendations. The tool flags potential wash-sale "
    "risks but cannot see trades made in accounts we do not have access to."
)

TAX_REPORT_DISCLOSURE = (
    "This report is generated from the lots and transactions in your account. "
    "Cost basis for lots imported from a broker CSV is as reported by that broker. "
    "Please reconcile against your broker's Form 1099-B before filing your return."
)
