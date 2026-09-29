from pydantic import BaseModel, Field, HttpUrl


class ImportWebsiteRequest(BaseModel):
    url: HttpUrl


class CreateManualBusinessRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class ImportWebsiteResponse(BaseModel):
    business_id: str
    pages_fetched: list[str]
    structured_info: dict


class BusinessReviewOut(BaseModel):
    id: str
    name: str
    website_url: str | None
    industry: str | None
    address: str | None
    description: str | None
    hours: dict | None
    phone: str | None
    status: str
    raw_import_snapshot: dict | None

    model_config = {"from_attributes": True}


class BusinessApproveRequest(BaseModel):
    """
    The edited/confirmed version of everything the importer found —
    spec §7: the customer edits before this becomes authoritative.
    Nothing here is written to knowledge_items until this endpoint is
    called; up to that point it's all still a draft.
    """
    name: str
    industry: str | None = None
    address: str | None = None
    description: str | None = None
    hours: dict | None = None
    phone: str | None = None
    services: list[str] = []
    faqs: list[dict] = []
    policies: list[str] = []