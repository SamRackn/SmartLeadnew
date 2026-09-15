"""Every boundary with a system we do not control.

Phase 1 is fixture-backed end to end: no API keys, no network. Vendor choices
(LinkedIn: Bright Data vs ScrapIn; company scraping: undecided) are deliberately
absent from every interface here.
"""

from mazz.integrations.base import (
    Fact,
    FactKind,
    build_fact,
    FetchResult,
    FetchStatus,
    IntegrationError,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    RetryPolicy,
)
from mazz.integrations.company import (
    CompanyAdapter,
    InvalidDomain,
    StubCompanyAdapter,
    get_company_adapter,
    normalize_domain,
)
from mazz.integrations.linkedin import (
    InvalidProfileURL,
    LinkedInAdapter,
    StubLinkedInAdapter,
    canonical_profile_url,
    get_linkedin_adapter,
)
from mazz.integrations.smartlead import (
    CampaignNotFound,
    LeadPushSettings,
    PushResult,
    SmartleadCampaign,
    SmartleadClient,
    SmartleadLead,
    StubSmartleadClient,
    get_smartlead_client,
)

__all__ = [
    "Fact",
    "FactKind",
    "build_fact",
    "FetchResult",
    "FetchStatus",
    "IntegrationError",
    "ProviderError",
    "ProviderUnavailable",
    "ProviderRateLimited",
    "ProviderAuthError",
    "ProviderBadResponse",
    "RetryPolicy",
    "LinkedInAdapter",
    "StubLinkedInAdapter",
    "InvalidProfileURL",
    "canonical_profile_url",
    "get_linkedin_adapter",
    "CompanyAdapter",
    "StubCompanyAdapter",
    "InvalidDomain",
    "normalize_domain",
    "get_company_adapter",
    "SmartleadClient",
    "StubSmartleadClient",
    "SmartleadLead",
    "SmartleadCampaign",
    "LeadPushSettings",
    "PushResult",
    "CampaignNotFound",
    "get_smartlead_client",
]
