"""
Lab report ID validation.

Validates lab report IDs against known formats for recognized labs.
"""

import re
from dataclasses import dataclass
from typing import Optional

import structlog

from config.allowed_sites import RECOGNIZED_LABS, is_recognized_lab

logger = structlog.get_logger(__name__)


@dataclass
class LabReportValidation:
    """Result of lab report validation."""

    is_valid: bool
    lab_name: Optional[str] = None
    report_id: Optional[str] = None
    normalized_lab: Optional[str] = None
    format_valid: bool = False
    message: str = ""


# Lab-specific report ID patterns
LAB_REPORT_PATTERNS = {
    "GIA": {
        "patterns": [
            r"^\d{10}$",  # Standard 10-digit
            r"^\d{7}$",  # Older 7-digit
        ],
        "description": "10 or 7 digit number",
    },
    "IGI": {
        "patterns": [
            r"^\d{9}$",  # Standard 9-digit
            r"^[A-Z]{2}\d{8}$",  # With country prefix
        ],
        "description": "9 digit number or 2-letter prefix + 8 digits",
    },
    "AGL": {
        "patterns": [
            r"^\d{7,8}$",  # 7-8 digit number
            r"^CS\d{5,6}$",  # CS prefix format
        ],
        "description": "7-8 digit number or CS prefix + 5-6 digits",
    },
    "SSEF": {
        "patterns": [
            r"^\d{5,8}$",  # Variable length number
        ],
        "description": "5-8 digit number",
    },
    "Gübelin": {
        "patterns": [
            r"^\d{7,10}$",  # Variable length number
        ],
        "description": "7-10 digit number",
    },
    "GUBELIN": {
        "patterns": [
            r"^\d{7,10}$",
        ],
        "description": "7-10 digit number",
    },
    "HRD": {
        "patterns": [
            r"^\d{10,15}$",  # Long number
        ],
        "description": "10-15 digit number",
    },
    "AGS": {
        "patterns": [
            r"^\d{10}$",  # 10-digit
        ],
        "description": "10 digit number",
    },
    "GCAL": {
        "patterns": [
            r"^\d{6,9}$",  # Variable length
        ],
        "description": "6-9 digit number",
    },
    "EGL": {
        "patterns": [
            r"^\d{9,12}$",  # Variable length
            r"^[A-Z]{2}\d{7,10}$",  # With prefix
        ],
        "description": "9-12 digit number or 2-letter prefix + 7-10 digits",
    },
}


def normalize_lab_name(lab_name: str) -> Optional[str]:
    """
    Normalize a lab name to canonical form.

    Args:
        lab_name: The lab name to normalize

    Returns:
        Normalized lab name or None if not recognized
    """
    if not lab_name:
        return None

    lab_upper = lab_name.upper().strip()

    # Direct matches
    if lab_upper in RECOGNIZED_LABS:
        return lab_upper

    # Handle variations
    variations = {
        "GEMOLOGICAL INSTITUTE OF AMERICA": "GIA",
        "INTERNATIONAL GEMOLOGICAL INSTITUTE": "IGI",
        "AMERICAN GEMOLOGICAL LABORATORIES": "AGL",
        "SWISS GEMMOLOGICAL INSTITUTE": "SSEF",
        "GÜBELIN GEM LAB": "Gübelin",
        "GUBELIN GEM LAB": "Gübelin",
        "HOGE RAAD VOOR DIAMANT": "HRD",
        "AMERICAN GEM SOCIETY": "AGS",
        "GEM CERTIFICATION AND ASSURANCE LAB": "GCAL",
        "EUROPEAN GEMOLOGICAL LABORATORY": "EGL",
    }

    for full_name, short in variations.items():
        if full_name in lab_upper or lab_upper in full_name:
            return short

    return None


def validate_report_id_format(lab_name: str, report_id: str) -> bool:
    """
    Validate that a report ID matches the expected format for a lab.

    Args:
        lab_name: The lab name
        report_id: The report ID to validate

    Returns:
        True if format is valid
    """
    if not lab_name or not report_id:
        return False

    # Normalize lab name
    normalized = normalize_lab_name(lab_name)
    if not normalized:
        return False

    # Clean report ID
    report_id = report_id.strip()

    # Get patterns for this lab
    lab_patterns = LAB_REPORT_PATTERNS.get(normalized)
    if not lab_patterns:
        # Unknown lab - accept any alphanumeric ID
        return bool(re.match(r"^[A-Za-z0-9\-]+$", report_id))

    # Check against lab-specific patterns
    for pattern in lab_patterns["patterns"]:
        if re.match(pattern, report_id):
            return True

    return False


def validate_lab_report_id(
    lab_name: Optional[str],
    report_id: Optional[str],
) -> LabReportValidation:
    """
    Validate a lab report ID.

    Args:
        lab_name: The lab name
        report_id: The report ID

    Returns:
        LabReportValidation with validation result
    """
    # No lab report
    if not lab_name and not report_id:
        return LabReportValidation(
            is_valid=False,
            message="No lab report information provided",
        )

    # Lab name but no ID
    if lab_name and not report_id:
        normalized = normalize_lab_name(lab_name)
        if normalized:
            return LabReportValidation(
                is_valid=False,
                lab_name=lab_name,
                normalized_lab=normalized,
                message=f"Lab {normalized} mentioned but no report ID found",
            )
        else:
            return LabReportValidation(
                is_valid=False,
                lab_name=lab_name,
                message=f"Unrecognized lab: {lab_name}",
            )

    # Report ID but no lab name
    if report_id and not lab_name:
        # Try to guess lab from ID format
        for lab, patterns in LAB_REPORT_PATTERNS.items():
            for pattern in patterns["patterns"]:
                if re.match(pattern, report_id.strip()):
                    return LabReportValidation(
                        is_valid=True,
                        report_id=report_id,
                        normalized_lab=lab,
                        format_valid=True,
                        message=f"Report ID format matches {lab}",
                    )

        return LabReportValidation(
            is_valid=False,
            report_id=report_id,
            message="Report ID provided but lab unknown and format unrecognized",
        )

    # Both lab name and report ID
    normalized = normalize_lab_name(lab_name)
    if not normalized:
        return LabReportValidation(
            is_valid=False,
            lab_name=lab_name,
            report_id=report_id,
            message=f"Unrecognized lab: {lab_name}",
        )

    format_valid = validate_report_id_format(lab_name, report_id)

    if format_valid:
        return LabReportValidation(
            is_valid=True,
            lab_name=lab_name,
            report_id=report_id,
            normalized_lab=normalized,
            format_valid=True,
            message=f"Valid {normalized} report: {report_id}",
        )
    else:
        return LabReportValidation(
            is_valid=False,
            lab_name=lab_name,
            report_id=report_id,
            normalized_lab=normalized,
            format_valid=False,
            message=f"Report ID {report_id} does not match expected format for {normalized}",
        )


def is_valid_lab_report(
    lab_name: Optional[str],
    report_id: Optional[str],
) -> bool:
    """
    Quick check if a lab report is valid.

    Args:
        lab_name: The lab name
        report_id: The report ID

    Returns:
        True if valid
    """
    result = validate_lab_report_id(lab_name, report_id)
    return result.is_valid
