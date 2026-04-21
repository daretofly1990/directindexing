from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..models.models import ESGExclusion, Position

ESG_SECTOR_RATINGS = {
    "Technology": {"score": 72, "grade": "B+", "concerns": ["E-waste", "Energy consumption"]},
    "Health Care": {"score": 75, "grade": "B+", "concerns": ["Drug pricing", "Access"]},
    "Financials": {"score": 65, "grade": "B", "concerns": ["Fossil fuel lending", "Executive pay"]},
    "Consumer Staples": {"score": 60, "grade": "B-", "concerns": ["Packaging waste", "Supply chain"]},
    "Consumer Discretionary": {"score": 58, "grade": "C+", "concerns": ["Labor practices", "Fast fashion"]},
    "Communication Services": {"score": 63, "grade": "B-", "concerns": ["Data privacy", "Misinformation"]},
    "Industrials": {"score": 55, "grade": "C+", "concerns": ["Carbon emissions", "Industrial waste"]},
    "Materials": {"score": 50, "grade": "C", "concerns": ["Mining impact", "Chemical pollution"]},
    "Energy": {"score": 35, "grade": "D", "concerns": ["Fossil fuels", "Carbon emissions", "Oil spills"]},
    "Real Estate": {"score": 60, "grade": "B-", "concerns": ["Building emissions"]},
    "Utilities": {"score": 52, "grade": "C+", "concerns": ["Coal power", "Nuclear waste"]},
}

class ESGService:

    async def get_exclusions(self, db: AsyncSession, portfolio_id: int) -> list[dict]:
        result = await db.execute(
            select(ESGExclusion).where(ESGExclusion.portfolio_id == portfolio_id)
        )
        return [
            {
                "id": e.id,
                "type": e.exclusion_type,
                "value": e.value,
                "reason": e.reason,
                "created_at": e.created_at.isoformat(),
            }
            for e in result.scalars().all()
        ]

    async def add_exclusion(
        self, db: AsyncSession, portfolio_id: int,
        exclusion_type: str, value: str, reason: str = None
    ) -> dict:
        exclusion = ESGExclusion(
            portfolio_id=portfolio_id,
            exclusion_type=exclusion_type.upper(),
            value=value.upper() if exclusion_type.upper() == "SYMBOL" else value,
            reason=reason,
        )
        db.add(exclusion)
        await db.commit()
        await db.refresh(exclusion)
        return {"id": exclusion.id, "type": exclusion.exclusion_type, "value": exclusion.value}

    async def remove_exclusion(self, db: AsyncSession, portfolio_id: int, exclusion_id: int):
        exc = await db.get(ESGExclusion, exclusion_id)
        if not exc or exc.portfolio_id != portfolio_id:
            raise ValueError("Exclusion not found")
        await db.delete(exc)
        await db.commit()

    async def get_portfolio_esg_analysis(self, db: AsyncSession, portfolio_id: int) -> dict:
        result = await db.execute(
            select(Position).where(
                Position.portfolio_id == portfolio_id,
                Position.is_active == True,
            )
        )
        positions = result.scalars().all()

        total_weight = sum(p.target_weight for p in positions)
        sector_dist: dict[str, dict] = {}
        weighted_score = 0.0
        all_concerns: set[str] = set()

        for pos in positions:
            sector = pos.sector or "Unknown"
            rating = ESG_SECTOR_RATINGS.get(sector, {"score": 50, "grade": "N/A", "concerns": []})
            w = pos.target_weight / total_weight if total_weight > 0 else 0
            weighted_score += rating["score"] * w
            all_concerns.update(rating["concerns"])
            if sector not in sector_dist:
                sector_dist[sector] = {"weight": 0.0, "grade": rating["grade"], "score": rating["score"]}
            sector_dist[sector]["weight"] += w

        exc_result = await db.execute(
            select(ESGExclusion).where(ESGExclusion.portfolio_id == portfolio_id)
        )
        exclusions = exc_result.scalars().all()

        return {
            "portfolio_esg_score": round(weighted_score, 1),
            "esg_grade": self._score_to_grade(weighted_score),
            "sector_distribution": sector_dist,
            "top_concerns": list(all_concerns)[:6],
            "active_exclusions": len(exclusions),
            "sector_ratings": ESG_SECTOR_RATINGS,
        }

    def _score_to_grade(self, score: float) -> str:
        if score >= 80: return "A"
        if score >= 70: return "B+"
        if score >= 60: return "B"
        if score >= 50: return "C+"
        if score >= 40: return "C"
        return "D"

esg_service = ESGService()
