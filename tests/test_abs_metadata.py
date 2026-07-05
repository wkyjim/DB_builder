from __future__ import annotations

from db_builder.abs_metadata import _codelist_id_from_urn, _format_abs_readable_name, parse_abs_structure


def test_codelist_id_from_urn_extracts_id():
    urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=ABS:CL_CPI_INDEX(1.0.0)"

    assert _codelist_id_from_urn(urn) == "CL_CPI_INDEX"


def test_parse_abs_structure_extracts_dimensions_and_codes():
    payload = {
        "dataStructures": [
            {
                "version": "1.0.0",
                "dataStructureComponents": {
                    "dimensionList": {
                        "dimensions": [
                            {
                                "id": "MEASURE",
                                "position": 0,
                                "localRepresentation": {
                                    "enumeration": "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=ABS:CL_TEST(1.0.0)"
                                },
                            }
                        ]
                    }
                },
            }
        ],
        "codelists": [
            {
                "id": "CL_TEST",
                "codes": [{"id": "1", "name": "Index numbers"}],
            }
        ],
    }

    dimensions, codes = parse_abs_structure("CPI", payload)

    assert dimensions[0]["dimension_id"] == "MEASURE"
    assert dimensions[0]["codelist_id"] == "CL_TEST"
    assert codes[0]["label"] == "Index numbers"


def test_format_abs_readable_name_uses_dimension_labels():
    decoded_dimensions = [
        {"dimension": "MEASURE", "label": "Percentage change from previous year"},
        {"dimension": "INDEX", "label": "All groups CPI"},
        {"dimension": "TSEST", "label": "Original"},
        {"dimension": "REGION", "label": "Australia"},
        {"dimension": "FREQ", "label": "Monthly"},
        {"dimension": "BASE_PERIOD", "label": "Sep 2025 = 100.0"},
    ]

    name = _format_abs_readable_name("CPI", decoded_dimensions)

    assert name == "ABS CPI - Percentage change from previous year - All groups CPI - Original - Australia - Monthly"


def test_format_abs_readable_name_includes_non_cpi_data_item():
    decoded_dimensions = [
        {"dimension": "MEASURE", "label": "Chain volume measures"},
        {"dimension": "DATA_ITEM", "label": "Gross domestic product"},
        {"dimension": "TSEST", "label": "Seasonally Adjusted"},
        {"dimension": "REGION", "label": "Australia"},
        {"dimension": "FREQ", "label": "Quarterly"},
    ]

    name = _format_abs_readable_name("ANA_AGG", decoded_dimensions)

    assert name == (
        "ABS National Accounts - Chain volume measures - Gross domestic product - "
        "Seasonally Adjusted - Australia - Quarterly"
    )
