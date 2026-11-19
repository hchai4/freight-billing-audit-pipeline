import os
from typing import List

import instructor
from openai import OpenAI
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

_client = None


def get_client():
    """Builds the OpenAI client on first use, so importing this module (and its
    prompt builder) needs no API key."""
    global _client
    if _client is None:
        _client = instructor.from_openai(OpenAI(api_key=os.getenv("OPENAI_API_KEY")))
    return _client


class DisputeRequest(BaseModel):
    """
    Input schema for requesting an automated dispute claim letter.
    """

    invoice_number: str = Field(description="Carrier invoice identifier")
    carrier_name: str = Field(
        description="Name of the shipping carrier (UPS, FedEx, DHL)"
    )
    tracking_id: str = Field(description="Package tracking number")
    billed_weight: float = Field(description="Weight billed by carrier in lbs")
    actual_weight: float = Field(
        description="Actual weight recorded by WMS scale in lbs"
    )
    billed_cost: float = Field(description="Total cost billed by carrier in USD")
    expected_cost: float = Field(
        description="Expected cost based on WMS scale weight in USD"
    )
    dollar_variance: float = Field(
        description="Overcharge amount requested for refund in USD"
    )
    anomaly_codes: str = Field(
        default="", description="Reason codes the audit assigned, separated by ' | '"
    )
    evidence: List[str] = Field(
        default_factory=list,
        description="Audit evidence lines, one fact per line. When present, the "
        "letter cites these instead of the weight comparison.",
    )


class DisputeResponse(BaseModel):
    """
    Output schema containing the generated dispute letter and metadata.
    """

    tracking_id: str
    dispute_letter: str = Field(
        description="Formal executive dispute letter to carrier billing support"
    )
    claim_amount: float = Field(description="Total dollar refund claimed")


SYSTEM_PROMPT = (
    "You are a professional logistics billing audit manager. "
    "Your job is to draft concise, firm, and formal carrier dispute letters "
    "requesting credit refunds for billing errors. "
    "Reference the exact invoice number, tracking ID, the evidence provided, "
    "and the requested refund amount. "
    "Use only the figures and facts supplied; never invent or recompute amounts, "
    "and request exactly the stated refund."
)


def build_user_prompt(req: DisputeRequest) -> str:
    """The letter brief. Evidence lines, when supplied, replace the weight comparison."""
    header = (
        f"Draft a formal dispute letter for Invoice #{req.invoice_number} "
        f"({req.carrier_name}).\n"
        f"- Tracking ID: {req.tracking_id}\n"
    )
    if req.evidence:
        findings = "\n".join(f"  - {line}" for line in req.evidence)
        return (
            header
            # Billed and expected totals are left out on purpose: for a late
            # delivery or a fee dispute they point the opposite way from the
            # refund and make the letter contradict itself.
            + f"- Refund requested: ${req.dollar_variance:,.2f}\n"
            + (f"- Reason codes: {req.anomaly_codes}\n" if req.anomaly_codes else "")
            + f"- Findings and evidence:\n{findings}"
        )
    return (
        header
        + f"- Carrier Billed Weight: {req.billed_weight} lbs (${req.billed_cost})\n"
        + f"- WMS Scale Recorded Weight: {req.actual_weight} lbs (${req.expected_cost})\n"
        + f"- Overcharge Refund Claim: ${req.dollar_variance}"
    )


class DisputeAgent:
    """
    Agent responsible for generating formal carrier dispute letters based on audit anomalies.
    """

    def __init__(self, model_name: str = "gpt-4o-mini"):
        self.model_name = model_name

    def generate_letter(self, req: DisputeRequest) -> DisputeResponse:
        system_prompt = SYSTEM_PROMPT
        user_prompt = build_user_prompt(req)

        response: DisputeResponse = get_client().chat.completions.create(
            model=self.model_name,
            response_model=DisputeResponse,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )

        return response
