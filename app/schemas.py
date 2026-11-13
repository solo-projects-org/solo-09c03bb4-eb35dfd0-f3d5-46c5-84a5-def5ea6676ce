"""请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class HarvestRequest(BaseModel):
    until_date: str = Field(
        ...,
        alias="until",
        description="采集截止日（含当天），格式 YYYY-MM-DD",
        examples=["2026-10-15"],
    )
    from_date: str | None = Field(
        default=None,
        alias="from",
        description=(
            "起始日（含当天），首次采集另填；之后留空则自动从上次成功"
            "采集的截止日续采"
        ),
        examples=["2026-10-01"],
    )

    model_config = {"populate_by_name": True}


class HarvestResponse(BaseModel):
    status: str = Field(..., description="success / failed")
    from_date: str = Field(..., description="实际使用的起始日（含）")
    until_date: str = Field(..., description="截止日（含）")
    pages: int = Field(..., description="本次成功拉取的页数")
    fetched: int = Field(..., description="拉取到的记录总数（含删除标记）")
    inserted: int
    updated: int
    withdrawn: int = Field(..., description="因删除标记撤下的记录数")
    skipped: int = Field(
        ..., description="按守卫规则忽略的更早/同日冲突记录数（清理过的标识）"
    )
    retries: int = Field(..., description="令牌失效后按原区间重抓的次数（0 或 1）")
    run_id: int
    note: str | None = None


class CleanupRequest(BaseModel):
    cutoff_date: str = Field(
        ...,
        alias="until",
        description="清理截止日（含当天），格式 YYYY-MM-DD；只处理 datestamp 不晚于该日且仍撤下的书目",
        examples=["2026-10-13"],
    )

    model_config = {"populate_by_name": True}


class CleanupResponse(BaseModel):
    cutoff_date: str = Field(..., description="实际使用的清理截止日（含当天）")
    count: int = Field(..., description="本次实际清掉元数据的记录数")
    cleared_identifiers: list[str] = Field(
        ..., description="本次实际清掉元数据的 OAI 标识（撤下记录本身保留）"
    )


class ProgressResponse(BaseModel):
    last_until: str | None = Field(
        None, description="上次成功采集的截止日（含）；无记录表示尚未采集"
    )
    last_harvest_at: str | None = None


class BibRecordOut(BaseModel):
    identifier: str
    datestamp: str
    status: str
    title: str | None
    creator: str | None
    date: str | None
    identifier_dc: str | None
    write_guarded: bool = Field(
        ..., description="元数据是否已被清理；为 true 时补采按库内 datestamp 守卫"
    )
    cleared_at: str | None = None
    updated_at: str


class RunOut(BaseModel):
    id: int
    started_at: str
    finished_at: str | None
    from_date: str
    until_date: str
    status: str
    pages: int
    fetched: int
    inserted: int
    updated: int
    withdrawn: int
    skipped: int
    retries: int
    error: str | None
