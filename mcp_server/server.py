"""MCP Server for writing confirmed reservations to file.

This server implements the Model Context Protocol (MCP) to provide a secure
interface for writing reservation data to persistent storage. It exposes
tools that can be called by the administrator agent when a reservation is
confirmed.

Stage 3 requirement: Write confirmed reservation details to a text file in
the format: Name | Car Number | Reservation Period | Approval Time

Security:
- Token-based authentication required for all write/read operations
- Constant-time token comparison to prevent timing attacks
- Input validation and sanitization to prevent injection attacks
- Safe serialization using structured format, not string concatenation
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import hmac
import json
import logging
import re

from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)

# Security: Token-based authentication loaded from environment
# Never log or expose this value
def _load_mcp_token() -> str:
    """Load MCP token securely from environment."""
    token = os.getenv("MCP_SERVER_TOKEN", "").strip()
    if not token:
        logger.warning(
            "MCP_SERVER_TOKEN not configured - server will reject all authenticated requests. "
            "Set MCP_SERVER_TOKEN in .env for production use."
        )
    return token

MCP_SERVER_TOKEN = _load_mcp_token()

# Data directory for file storage (configurable via environment variable)
MCP_DATA_DIR = Path(os.getenv("MCP_DATA_DIR", "."))
MCP_DATA_DIR.mkdir(parents=True, exist_ok=True)  # Ensure directory exists
RESERVATIONS_FILE = MCP_DATA_DIR / "confirmed_reservations.txt"

app = FastAPI(
    title="CityPark MCP Server",
    description="MCP server for processing and storing confirmed vehicle reservations",
    version="1.0.0",
)


class ReservationData(BaseModel):
    """Reservation data model for confirmed bookings with input validation."""

    reservation_id: int = Field(..., description="Unique reservation ID", gt=0)
    first_name: str = Field(..., description="Customer first name", min_length=1, max_length=100)
    last_name: str = Field(..., description="Customer last name", min_length=1, max_length=100)
    car_number: str = Field(..., description="Vehicle license plate number", min_length=1, max_length=20)
    start_ts: str = Field(..., description="Reservation start timestamp", min_length=1)
    end_ts: str = Field(..., description="Reservation end timestamp", min_length=1)
    approved_by: str = Field(default="admin", description="Administrator who approved", max_length=100)
    approval_time: Optional[str] = Field(
        default=None, description="Approval timestamp (ISO format)"
    )

    @field_validator('first_name', 'last_name', 'approved_by')
    @classmethod
    def validate_no_newlines_or_delimiters(cls, v: str) -> str:
        """Prevent newline and delimiter injection in text fields."""
        if '\n' in v or '\r' in v or '|' in v:
            raise ValueError("Field cannot contain newlines or pipe characters")
        return v.strip()

    @field_validator('car_number')
    @classmethod
    def validate_car_number(cls, v: str) -> str:
        """Validate car number format and prevent injection."""
        v = v.strip()
        if '\n' in v or '\r' in v or '|' in v:
            raise ValueError("Car number cannot contain newlines or pipe characters")
        # Basic validation: alphanumeric with hyphens/spaces
        if not re.match(r'^[A-Za-z0-9\s\-]{1,20}$', v):
            raise ValueError("Invalid car number format")
        return v

    @field_validator('start_ts', 'end_ts', 'approval_time')
    @classmethod
    def validate_timestamp(cls, v: Optional[str]) -> Optional[str]:
        """Validate timestamp format."""
        if v is None:
            return v
        v = v.strip()
        if '\n' in v or '\r' in v or '|' in v:
            raise ValueError("Timestamp cannot contain newlines or pipe characters")
        # Basic ISO format check
        if not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}', v):
            raise ValueError("Invalid timestamp format (expected ISO 8601)")
        return v


class WriteResult(BaseModel):
    """Result of write operation."""

    success: bool
    message: str
    file_path: str
    entry_count: int


def verify_token(x_mcp_token: Optional[str] = Header(None)) -> bool:
    """Verify MCP server token for authentication.

    Security requirements:
    - Require authentication for all protected operations
    - Use constant-time comparison to prevent timing attacks
    - Reject missing, invalid, or empty tokens
    """
    # Reject access when no token is configured (fail-secure)
    if not MCP_SERVER_TOKEN:
        logger.error("Authentication failed: MCP_SERVER_TOKEN not configured on server")
        raise HTTPException(
            status_code=503,
            detail="Server configuration error: authentication not available"
        )

    # Reject missing or empty tokens
    if not x_mcp_token or not x_mcp_token.strip():
        logger.warning("Authentication failed: missing or empty token")
        raise HTTPException(
            status_code=401,
            detail="Authentication required: X-MCP-Token header missing"
        )

    # Use constant-time comparison to prevent timing attacks
    token_bytes = x_mcp_token.strip().encode('utf-8')
    expected_bytes = MCP_SERVER_TOKEN.encode('utf-8')

    if not hmac.compare_digest(token_bytes, expected_bytes):
        logger.warning("Authentication failed: invalid token")
        raise HTTPException(status_code=401, detail="Invalid MCP token")

    return True


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "citypark-mcp-server",
        "version": "1.0.0",
        "file_path": str(RESERVATIONS_FILE.absolute()),
        "file_exists": RESERVATIONS_FILE.exists(),
    }


@app.get("/tools/list")
async def list_tools(authorized: bool = Depends(verify_token)):
    """MCP protocol: List available tools.

    Returns the tool definitions that can be invoked by the administrator agent.
    """
    return {
        "tools": [
            {
                "name": "write_confirmed_reservation",
                "description": "Write a confirmed reservation to persistent storage",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reservation_id": {"type": "integer"},
                        "first_name": {"type": "string"},
                        "last_name": {"type": "string"},
                        "car_number": {"type": "string"},
                        "start_ts": {"type": "string"},
                        "end_ts": {"type": "string"},
                        "approved_by": {"type": "string"},
                        "approval_time": {"type": "string"},
                    },
                    "required": [
                        "reservation_id",
                        "first_name",
                        "last_name",
                        "car_number",
                        "start_ts",
                        "end_ts",
                    ],
                },
            },
            {
                "name": "read_confirmed_reservations",
                "description": "Read all confirmed reservations from storage",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]
    }


def _format_reservation_entry(data: ReservationData, approval_time: str) -> str:
    """Format a reservation as a file entry using safe serialization.

    Format: #ID | Name | Car Number | Reservation Period | Approval Time

    All fields are validated to prevent newline/delimiter injection.
    """
    # Validated fields (validators already stripped and checked for delimiters)
    full_name = f"{data.first_name} {data.last_name}"
    reservation_period = f"{data.start_ts} to {data.end_ts}"

    # Build entry with explicit field order, including reservation_id for duplicate detection
    return f"#{data.reservation_id} | {full_name} | {data.car_number} | {reservation_period} | {approval_time}\n"


def _is_reservation_already_written(reservation_id: int) -> bool:
    """Check if a reservation has already been written (idempotency check).

    Parses the file to check if the reservation_id is already present.
    File format: #ID | Name | Car Number | Reservation Period | Approval Time
    """
    if not RESERVATIONS_FILE.exists():
        return False

    try:
        with RESERVATIONS_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Check if line starts with #ID
                if line.startswith(f"#{reservation_id} |"):
                    logger.info("Reservation #%s already exists in file (idempotent)", reservation_id)
                    return True
        return False
    except Exception as e:
        logger.warning("Failed to check if reservation exists: %s", e)
        # Conservative: assume it doesn't exist to allow the write attempt
        return False


@app.post("/tools/call/write_confirmed_reservation")
async def write_confirmed_reservation(
    data: ReservationData, authorized: bool = Depends(verify_token)
) -> WriteResult:
    """MCP tool: Write a confirmed reservation to file.

    Format: #ID | Name | Car Number | Reservation Period | Approval Time

    This tool is called by the escalation service after the administrator
    approves a reservation.

    Security:
    - Input validation prevents injection attacks
    - Safe serialization using validated fields
    - Idempotent: duplicate detection checks if reservation_id already exists
    """
    try:
        # Check if already written (idempotency)
        if _is_reservation_already_written(data.reservation_id):
            # Count total entries
            entry_count = 0
            if RESERVATIONS_FILE.exists():
                with RESERVATIONS_FILE.open("r", encoding="utf-8") as f:
                    entry_count = sum(1 for line in f if line.strip())

            logger.info(
                "Reservation #%s already written (idempotent), skipping duplicate",
                data.reservation_id,
            )
            return WriteResult(
                success=True,
                message=f"Reservation #{data.reservation_id} already exists (idempotent)",
                file_path=str(RESERVATIONS_FILE.absolute()),
                entry_count=entry_count,
            )

        # Set approval time if not provided
        approval_time = data.approval_time or datetime.now(timezone.utc).isoformat()

        # Validate approval time format
        if '\n' in approval_time or '\r' in approval_time or '|' in approval_time:
            raise ValueError("Invalid approval time format")

        # Format entry using safe serialization
        entry = _format_reservation_entry(data, approval_time)

        # Append to file (create if doesn't exist)
        # File operations are atomic at the OS level for appends
        with RESERVATIONS_FILE.open("a", encoding="utf-8") as f:
            f.write(entry)

        # Count total entries
        entry_count = 0
        if RESERVATIONS_FILE.exists():
            with RESERVATIONS_FILE.open("r", encoding="utf-8") as f:
                entry_count = sum(1 for line in f if line.strip())

        logger.info(
            "Wrote confirmed reservation #%s to %s",
            data.reservation_id,
            RESERVATIONS_FILE,
        )

        return WriteResult(
            success=True,
            message=f"Reservation #{data.reservation_id} written successfully",
            file_path=str(RESERVATIONS_FILE.absolute()),
            entry_count=entry_count,
        )

    except ValueError as e:
        logger.error("Validation error for reservation #%s: %s", data.reservation_id, e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Failed to write reservation #%s: %s", data.reservation_id, e, exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to write reservation: {str(e)}"
        )


@app.post("/tools/call/read_confirmed_reservations")
async def read_confirmed_reservations(authorized: bool = Depends(verify_token)):
    """MCP tool: Read all confirmed reservations.

    Returns the contents of the confirmed reservations file.
    """
    try:
        if not RESERVATIONS_FILE.exists():
            return {
                "success": True,
                "entries": [],
                "count": 0,
                "message": "No confirmed reservations yet",
            }

        with RESERVATIONS_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()

        entries = [line.strip() for line in lines if line.strip()]

        return {
            "success": True,
            "entries": entries,
            "count": len(entries),
            "file_path": str(RESERVATIONS_FILE.absolute()),
        }

    except Exception as e:
        logger.error("Failed to read reservations: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to read reservations: {str(e)}"
        )


@app.get("/reservations")
async def get_reservations_summary(authorized: bool = Depends(verify_token)):
    """Get a summary of all confirmed reservations."""
    try:
        if not RESERVATIONS_FILE.exists():
            return {"count": 0, "file_exists": False, "entries": []}

        with RESERVATIONS_FILE.open("r", encoding="utf-8") as f:
            entries = [line.strip() for line in f if line.strip()]

        return {
            "count": len(entries),
            "file_exists": True,
            "file_path": str(RESERVATIONS_FILE.absolute()),
            "entries": entries[-10:],  # Last 10 entries
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="info")
