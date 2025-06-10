from datetime import datetime
import json
import logging
import os
from typing import Dict, List, Optional, Any
from urllib.parse import unquote
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator
from werkzeug.datastructures import MultiDict
from uuid import UUID
from geovisio import errors, utils
from geovisio.utils import auth, db
from geovisio.utils.params import validation_error
from geovisio.utils.pictures import cleanupExif
from geovisio.utils.semantics import SemanticTagUpdate, Entity, EntityType, update_tags
import psycopg2
from geovisio.web.params import (
    as_latitude,
    as_longitude,
    as_uuid,
    parse_datetime,
    parse_datetime_interval,
    parse_bbox,
    parse_list,
    parse_lonlat,
    parse_distance_range,
    parse_picture_heading,
)
from geovisio.utils.fields import Bounds
import hashlib
from psycopg.rows import dict_row
from psycopg.sql import SQL
from geovisio.web.utils import (
    accountIdOrDefault,
    cleanNoneInList,
    dbTsToStac,
    dbTsToStacTZ,
    get_license_link,
    get_root_link,
    removeNoneInDict,
    STAC_VERSION,
)
from flask import current_app, request, url_for, Blueprint
from flask_babel import gettext as _, get_locale
from geopic_tag_reader.writer import writePictureMetadata, PictureMetadata
import sentry_sdk
import math
import psycopg


bp = Blueprint("stac_items", __name__, url_prefix="/api")


def dbPictureToStacItem(seqId, dbPic):
    """Transforms a picture extracted from database into a STAC Item

    Parameters
    ----------
    seqId : uuid
        Associated sequence ID
    dbPic : dict
        A row from pictures table in database (with id, geojson, ts, heading, cols, rows, width, height, prevpic, nextpic, prevpicgeojson, nextpicgeojson, exif fields)

    Returns
    -------
    object
        The equivalent in STAC Item format
    """

    sensorDim = None
    visibleArea = None
    if dbPic["metadata"].get("crop") is not None:
        sensorDim = [dbPic["metadata"]["crop"].get("fullWidth"), dbPic["metadata"]["crop"].get("fullHeight")]
        visibleArea = [
            dbPic["metadata"]["crop"].get("left"),
            dbPic["metadata"]["crop"].get("top"),
            int(dbPic["metadata"]["crop"].get("fullWidth", "0"))
            - int(dbPic["metadata"]["crop"].get("width", "0"))
            - int(dbPic["metadata"]["crop"].get("left", "0")),
            int(dbPic["metadata"]["crop"].get("fullHeight", "0"))
            - int(dbPic["metadata"]["crop"].get("height", "0"))
            - int(dbPic["metadata"]["crop"].get("top", "0")),
        ]
        if None in sensorDim:
            sensorDim = None
        if None in visibleArea or visibleArea == [0, 0, 0, 0]:
            visibleArea = None
    elif "height" in dbPic["metadata"] and "width" in dbPic["metadata"]:
        sensorDim = [dbPic["metadata"]["width"], dbPic["metadata"]["height"]]

    item = removeNoneInDict(
        {
            "type": "Feature",
            "stac_version": STAC_VERSION,
            "stac_extensions": [
                "https://stac-extensions.github.io/view/v1.0.0/schema.json",  # "view:" fields
                "https://stac-extensions.github.io/perspective-imagery/v1.0.0/schema.json",  # "pers:" fields
            ],
            "id": str(dbPic["id"]),
            "geometry": dbPic["geojson"],
            "bbox": dbPic["geojson"]["coordinates"] + dbPic["geojson"]["coordinates"],
            "providers": cleanNoneInList(
                [
                    {"name": dbPic["account_name"], "roles": ["producer"], "id": str(dbPic["account_id"])},
                    (
                        {"name": dbPic["exif"]["Exif.Image.Artist"], "roles": ["producer"]}
                        if dbPic["exif"].get("Exif.Image.Artist") is not None
                        else None
                    ),
                ]
            ),
            "properties": removeNoneInDict(
                {
                    "datetime": dbTsToStac(dbPic["ts"]),
                    "datetimetz": dbTsToStacTZ(dbPic["ts"], dbPic["metadata"].get("tz")),
                    "created": dbTsToStac(dbPic["inserted_at"]),
                    # TODO : add "updated" TS for last edit time of metadata
                    "license": current_app.config["API_PICTURES_LICENSE_SPDX_ID"],
                    "view:azimuth": dbPic["heading"],
                    "pers:interior_orientation": (
                        removeNoneInDict(
                            {
                                "camera_manufacturer": dbPic["metadata"].get("make"),
                                "camera_model": dbPic["metadata"].get("model"),
                                "focal_length": dbPic["metadata"].get("focal_length"),
                                "field_of_view": dbPic["metadata"].get("field_of_view"),
                                "sensor_array_dimensions": sensorDim,
                                "visible_area": visibleArea,
                            }
                        )
                        if "metadata" in dbPic
                        and any(
                            True
                            for f in dbPic["metadata"]
                            if f in ["make", "model", "focal_length", "field_of_view", "crop", "width", "height"]
                        )
                        else {}
                    ),
                    "pers:pitch": dbPic["metadata"].get("pitch"),
                    "pers:roll": dbPic["metadata"].get("roll"),
                    "geovisio:status": dbPic.get("status"),
                    "geovisio:producer": dbPic["account_name"],
                    "original_file:size": dbPic["metadata"].get("originalFileSize"),
                    "original_file:name": dbPic["metadata"].get("originalFileName"),
                    "panoramax:horizontal_pixel_density": dbPic.get("h_pixel_density"),
                    "geovisio:image": _getHDJpgPictureURL(dbPic["id"], dbPic.get("status")),
                    "geovisio:thumbnail": _getThumbJpgPictureURL(dbPic["id"], dbPic.get("status")),
                    "exif": removeNoneInDict(cleanupExif(dbPic["exif"])),
                    "quality:horizontal_accuracy": float("{:.1f}".format(dbPic["gps_accuracy_m"])) if dbPic.get("gps_accuracy_m") else None,
                    "semantics": dbPic["semantics"] if "semantics" in dbPic else None,
                }
            ),
            "links": cleanNoneInList(
                [
                    get_root_link(),
                    {
                        "rel": "parent",
                        "type": "application/json",
                        "href": url_for("stac_collections.getCollection", _external=True, collectionId=seqId),
                    },
                    {
                        "rel": "self",
                        "type": "application/geo+json",
                        "href": url_for("stac_items.getCollectionItem", _external=True, collectionId=seqId, itemId=dbPic["id"]),
                    },
                    {
                        "rel": "collection",
                        "type": "application/json",
                        "href": url_for("stac_collections.getCollection", _external=True, collectionId=seqId),
                    },
                    get_license_link(),
                ]
            ),
            "assets": {
                "hd": {
                    "title": "HD picture",
                    "description": "Highest resolution available of this picture",
                    "roles": ["data"],
                    "type": "image/jpeg",
                    "href": _getHDJpgPictureURL(dbPic["id"], status=dbPic.get("status")),
                },
                "sd": {
                    "title": "SD picture",
                    "description": "Picture in standard definition (fixed width of 2048px)",
                    "roles": ["visual"],
                    "type": "image/jpeg",
                    "href": _getSDJpgPictureURL(dbPic["id"], status=dbPic.get("status")),
                },
                "thumb": {
                    "title": "Thumbnail",
                    "description": "Picture in low definition (fixed width of 500px)",
                    "roles": ["thumbnail"],
                    "type": "image/jpeg",
                    "href": _getThumbJpgPictureURL(dbPic["id"], status=dbPic.get("status")),
                },
            },
            "collection": str(seqId),
        }
    )

    # Next / previous links if any
    if "nextpic" in dbPic and dbPic["nextpic"] is not None:
        item["links"].append(
            {
                "rel": "next",
                "type": "application/geo+json",
                "geometry": dbPic["nextpicgeojson"],
                "id": dbPic["nextpic"],
                "href": url_for("stac_items.getCollectionItem", _external=True, collectionId=seqId, itemId=dbPic["nextpic"]),
            }
        )

    if "prevpic" in dbPic and dbPic["prevpic"] is not None:
        item["links"].append(
            {
                "rel": "prev",
                "type": "application/geo+json",
                "geometry": dbPic["prevpicgeojson"],
                "id": dbPic["prevpic"],
                "href": url_for("stac_items.getCollectionItem", _external=True, collectionId=seqId, itemId=dbPic["prevpic"]),
            }
        )

    if dbPic.get("related_pics") is not None:
        for rp in dbPic["related_pics"]:
            repSeq, rpId, rpGeom, rpTs = rp
            item["links"].append(
                {
                    "rel": "related",
                    "type": "application/geo+json",
                    "geometry": json.loads(rpGeom),
                    "datetime": rpTs,
                    "id": rpId,
                    "href": url_for("stac_items.getCollectionItem", _external=True, collectionId=repSeq, itemId=rpId),
                }
            )

    #
    # Picture type-specific properties
    #

    # Equirectangular
    if dbPic["metadata"]["type"] == "equirectangular":
        item["stac_extensions"].append("https://stac-extensions.github.io/tiled-assets/v1.0.0/schema.json")  # "tiles:" fields

        item["properties"]["tiles:tile_matrix_sets"] = {
            "geovisio": {
                "type": "TileMatrixSetType",
                "title": "GeoVisio tile matrix for picture " + str(dbPic["id"]),
                "identifier": "geovisio-" + str(dbPic["id"]),
                "tileMatrix": [
                    {
                        "type": "TileMatrixType",
                        "identifier": "0",
                        "scaleDenominator": 1,
                        "topLeftCorner": [0, 0],
                        "tileWidth": dbPic["metadata"]["width"] / dbPic["metadata"]["cols"],
                        "tileHeight": dbPic["metadata"]["height"] / dbPic["metadata"]["rows"],
                        "matrixWidth": dbPic["metadata"]["cols"],
                        "matrixHeight": dbPic["metadata"]["rows"],
                    }
                ],
            }
        }

        item["asset_templates"] = {
            "tiles": {
                "title": "HD tiled picture",
                "description": "Highest resolution available of this picture, as tiles",
                "roles": ["data"],
                "type": "image/jpeg",
                "href": _getTilesJpgPictureURL(dbPic["id"], status=dbPic.get("status")),
            }
        }

    return item


def get_first_rank_of_page(rankToHave: int, limit: Optional[int]) -> int:
    """if there is a limit, we try to emulate a page, so we'll return the nth page that should contain this picture
    Note: the ranks starts from 1
    >>> get_first_rank_of_page(3, 2)
    3
    >>> get_first_rank_of_page(4, 2)
    3
    >>> get_first_rank_of_page(3, None)
    3
    >>> get_first_rank_of_page(123, 10)
    121
    >>> get_first_rank_of_page(10, 10)
    1
    >>> get_first_rank_of_page(10, 100)
    1
    """
    if not limit:
        return rankToHave

    return int((rankToHave - 1) / limit) * limit + 1


@bp.route("/collections/<uuid:collectionId>/items", methods=["GET"])
def getCollectionItems(collectionId):
    """List items of a single collection
    ---
    tags:
        - Sequences
    parameters:
        - name: collectionId
          in: path
          description: ID of collection to retrieve
          required: true
          schema:
            type: string
        - name: limit
          in: query
          description: Number of items that should be present in response. Unlimited by default.
          required: false
          schema:
            type: integer
            minimum: 1
            maximum: 10000
        - name: startAfterRank
          in: query
          description: Position of last received picture in sequence. Response will start with the following picture.
          required: false
          schema:
            type: integer
            minimum: 1
        - name: withPicture
          in: query
          description: Used in the pagination context, if present, the api will return the given picture in the results.
            Can be used in the same time as the `limit` parameter, but not with the `startAfterRank` parameter.
          required: false
          schema:
            type: string
            format: uuid
    responses:
        200:
            description: the items list
            content:
                application/geo+json:
                    schema:
                        $ref: '#/components/schemas/GeoVisioCollectionItems'
    """

    account = auth.get_current_account()

    params = {
        "seq": collectionId,
        # Only the owner of an account can view pictures not 'ready'
        "account": account.id if account is not None else None,
    }

    args = request.args
    limit = args.get("limit")
    startAfterRank = args.get("startAfterRank")
    withPicture = args.get("withPicture")

    filters = [
        SQL("sp.seq_id = %(seq)s"),
        SQL("(p.status = 'ready' OR p.account_id = %(account)s)"),
        SQL("(is_sequence_visible_by_user(s, %(account)s))"),
    ]

    # Check if limit is valid
    sql_limit = SQL("")
    if limit is not None:
        try:
            limit = int(limit)
            if limit < 1 or limit > 10000:
                raise errors.InvalidAPIUsage(_("limit parameter should be an integer between 1 and 10000"), status_code=400)
        except ValueError:
            raise errors.InvalidAPIUsage(_("limit parameter should be a valid, positive integer (between 1 and 10000)"), status_code=400)
        sql_limit = SQL("LIMIT %(limit)s")
        params["limit"] = limit

    if withPicture and startAfterRank:
        raise errors.InvalidAPIUsage(_("`startAfterRank` and `withPicture` are mutually exclusive parameters"))

    # Check if rank is valid
    if startAfterRank is not None:
        try:
            startAfterRank = int(startAfterRank)
            if startAfterRank < 1:
                raise errors.InvalidAPIUsage(_("startAfterRank parameter should be a positive integer (starting from 1)"), status_code=400)
        except ValueError:
            raise errors.InvalidAPIUsage(_("startAfterRank parameter should be a valid, positive integer"), status_code=400)

        filters.append(SQL("rank > %(start_after_rank)s"))
        params["start_after_rank"] = startAfterRank

    paginated = startAfterRank is not None or limit is not None or withPicture is not None
    current_app.config["DB_URL"]="postgresql://postgres:postgres@localhost/Panaramax"
    with psycopg.connect(current_app.config["DB_URL"], row_factory=dict_row) as conn:
        with conn.cursor() as cursor:
            # check on sequence
            seqMeta = cursor.execute(
                "SELECT s.id "
                + (", MAX(sp.rank) AS max_rank, MIN(sp.rank) AS min_rank " if paginated else "")
                + "FROM sequences s "
                + ("LEFT JOIN sequences_pictures sp ON sp.seq_id = s.id " if paginated else "")
                + "WHERE s.id = %(seq)s AND (is_sequence_visible_by_user(s, %(account)s)) "
                + ("GROUP BY s.id" if paginated else ""),
                params,
            ).fetchone()

            if seqMeta is None:
                raise errors.InvalidAPIUsage(_("Collection doesn't exist"), status_code=404)

            maxRank = seqMeta.get("max_rank")

            if startAfterRank is not None and startAfterRank >= maxRank:
                raise errors.InvalidAPIUsage(
                    _("No more items in this collection (last available rank is %(r)s)", r=maxRank), status_code=404
                )

            if withPicture is not None:
                withPicture = as_uuid(withPicture, "withPicture should be a valid UUID")
                pic = cursor.execute(
                    "SELECT rank FROM pictures p JOIN sequences_pictures sp ON sp.pic_id = p.id WHERE p.id = %(id)s AND sp.seq_id = %(seq)s",
                    params={"id": withPicture, "seq": collectionId},
                ).fetchone()
                if not pic:
                    raise errors.InvalidAPIUsage(_("Picture with id %(p)s does not exists", p=withPicture))
                rank = get_first_rank_of_page(pic["rank"], limit)

                filters.append(SQL("rank >= %(start_after_rank)s"))
                params["start_after_rank"] = rank

            query = SQL(
                """
                SELECT
                    p.id, p.ts, p.heading, p.metadata, p.inserted_at, p.status,
                    ST_AsGeoJSON(p.geom)::json AS geojson,
                    a.name AS account_name,
                    p.account_id AS account_id,
                    sp.rank, p.exif, p.gps_accuracy_m, p.h_pixel_density,
                    CASE WHEN LAG(p.status) OVER othpics = 'ready' THEN LAG(p.id) OVER othpics END AS prevpic,
                    CASE WHEN LAG(p.status) OVER othpics = 'ready' THEN ST_AsGeoJSON(LAG(p.geom) OVER othpics)::json END AS prevpicgeojson,
                    CASE WHEN LEAD(p.status) OVER othpics = 'ready' THEN LEAD(p.id) OVER othpics END AS nextpic,
                    CASE WHEN LEAD(p.status) OVER othpics = 'ready' THEN ST_AsGeoJSON(LEAD(p.geom) OVER othpics)::json END AS nextpicgeojson,
                    t.semantics
                FROM sequences_pictures sp
                JOIN pictures p ON sp.pic_id = p.id
                JOIN accounts a ON a.id = p.account_id
                JOIN sequences s ON s.id = sp.seq_id
                LEFT JOIN (
                    SELECT picture_id, json_agg(json_strip_nulls(json_build_object(
                        'key', key,
                        'value', value
                    ))) AS semantics
                    FROM pictures_semantics
                    GROUP BY picture_id
                ) t ON t.picture_id = p.id
                WHERE
                    {filter}
                WINDOW othpics AS (PARTITION BY sp.seq_id ORDER BY sp.rank)
                ORDER BY rank
                {limit}
                """
            ).format(filter=SQL(" AND ").join(filters), limit=sql_limit)

            records = cursor.execute(query, params)

            bounds: Optional[Bounds] = None
            items = []
            for dbPic in records:
                if not bounds:
                    bounds = Bounds(min=dbPic["rank"], max=dbPic["rank"])
                else:
                    bounds.update(dbPic["rank"])

                items.append(dbPictureToStacItem(collectionId, dbPic))

            links = [
                get_root_link(),
                {
                    "rel": "parent",
                    "type": "application/json",
                    "href": url_for("stac_collections.getCollection", _external=True, collectionId=collectionId),
                },
                {
                    "rel": "self",
                    "type": "application/geo+json",
                    "href": url_for(
                        "stac_items.getCollectionItems",
                        _external=True,
                        collectionId=collectionId,
                        limit=limit,
                        startAfterRank=startAfterRank,
                    ),
                },
            ]

            if paginated and items and bounds:
                if bounds.min:
                    has_item_before = bounds.min > seqMeta["min_rank"]
                    if has_item_before:
                        links.append(
                            {
                                "rel": "first",
                                "type": "application/geo+json",
                                "href": url_for("stac_items.getCollectionItems", _external=True, collectionId=collectionId, limit=limit),
                            }
                        )
                        # Previous page link
                        #   - If limit is set, rank is current - limit -1
                        #   - If no limit is set, rank is 0 (none)
                        prevRank = bounds.min - limit - 1 if limit is not None else 0
                        if prevRank < 1:
                            prevRank = None
                        links.append(
                            {
                                "rel": "prev",
                                "type": "application/geo+json",
                                "href": url_for(
                                    "stac_items.getCollectionItems",
                                    _external=True,
                                    collectionId=collectionId,
                                    limit=limit,
                                    startAfterRank=prevRank,
                                ),
                            }
                        )

                has_item_after = bounds.max < seqMeta["max_rank"]
                if has_item_after:
                    links.append(
                        {
                            "rel": "next",
                            "type": "application/geo+json",
                            "href": url_for(
                                "stac_items.getCollectionItems",
                                _external=True,
                                collectionId=collectionId,
                                limit=limit,
                                startAfterRank=bounds.max,
                            ),
                        }
                    )

                    # Last page link
                    #   - If this page is the last one, rank equals to rank given by user
                    #   - Otherwise, rank equals max rank - limit

                    lastPageRank = startAfterRank
                    if limit is not None:
                        if seqMeta["max_rank"] > bounds.max:
                            lastPageRank = seqMeta["max_rank"] - limit
                            if lastPageRank < bounds.max:
                                lastPageRank = bounds.max

                    links.append(
                        {
                            "rel": "last",
                            "type": "application/geo+json",
                            "href": url_for(
                                "stac_items.getCollectionItems",
                                _external=True,
                                collectionId=collectionId,
                                limit=limit,
                                startAfterRank=lastPageRank,
                            ),
                        }
                    )

            return (
                {
                    "type": "FeatureCollection",
                    "features": items,
                    "links": links,
                },
                200,
                {"Content-Type": "application/geo+json"},
            )


def _getPictureItemById(collectionId, itemId):
    """Get a picture metadata by its ID and collection ID

    ---
    tags:
        - Pictures
    parameters:
        - name: collectionId
          in: path
          description: ID of collection to retrieve
          required: true
          schema:
            type: string
        - name: itemId
          in: path
          description: ID of item to retrieve
          required: true
          schema:
            type: string
    """
    with current_app.pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cursor:
            # Check if there is a logged user
            account = auth.get_current_account()
            accountId = account.id if account else None

            # Get rank + position of wanted picture
            record = cursor.execute(
                """
                SELECT
                    p.id, sp.rank, ST_AsGeoJSON(p.geom)::json AS geojson, p.heading, p.ts, p.metadata,
                    p.inserted_at, p.status, accounts.name AS account_name,
                    p.account_id AS account_id,
                    spl.prevpic, spl.prevpicgeojson, spl.nextpic, spl.nextpicgeojson, p.exif,
                    relp.related_pics, p.gps_accuracy_m, p.h_pixel_density,
                    t.semantics
                FROM pictures p
                JOIN sequences_pictures sp ON sp.pic_id = p.id
                JOIN accounts ON p.account_id = accounts.id
                JOIN sequences s ON sp.seq_id = s.id
                LEFT JOIN (
                    SELECT picture_id, json_agg(json_strip_nulls(json_build_object(
                        'key', key,
                        'value', value
                    ))) AS semantics
                    FROM pictures_semantics
                    GROUP BY picture_id
                ) t ON t.picture_id = p.id
                LEFT JOIN (
                    SELECT
                        p.id,
                        LAG(p.id) OVER othpics AS prevpic,
                        ST_AsGeoJSON(LAG(p.geom) OVER othpics)::json AS prevpicgeojson,
                        LEAD(p.id) OVER othpics AS nextpic,
                        ST_AsGeoJSON(LEAD(p.geom) OVER othpics)::json AS nextpicgeojson
                    FROM pictures p
                    JOIN sequences_pictures sp ON p.id = sp.pic_id
                    WHERE
                        sp.seq_id = %(seq)s
                        AND (p.account_id = %(acc)s OR p.status != 'hidden')
                    WINDOW othpics AS (PARTITION BY sp.seq_id ORDER BY sp.rank)
                ) spl ON p.id = spl.id
                LEFT JOIN (
                    SELECT array_agg(ARRAY[seq_id::text, id::text, geom, tstxt]) AS related_pics
                    FROM (
                        SELECT DISTINCT ON (relsp.seq_id)
                            relsp.seq_id, relp.id,
                            ST_AsGeoJSON(relp.geom) as geom,
                            to_char(relp.ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS tstxt
                        FROM
                            pictures relp,
                            pictures p,
                            sequences_pictures relsp
                        WHERE
                            -- Related pictures are retrieved based on:
                            --   > Proximity (15m)
                            --   > Status (publicly available or from current user)
                            --   > Sequence (only one per sequence, the nearest one)
                            --   > Pic ID (not the current picture)
                            --   > Heading (either 360° or in less than 100° of diff with current picture)
                            p.id = %(pic)s
                            AND ST_Intersects(ST_Buffer(p.geom::geography, 15)::geometry, relp.geom)
                            AND (relp.account_id = %(acc)s OR relp.status = 'ready')
                            AND relp.status != 'waiting-for-delete'
                            AND relp.id != p.id
                            AND relsp.pic_id = relp.id
                            AND relsp.seq_id != %(seq)s
                            AND (
                                p.metadata->>'type' = 'equirectangular'
                                OR (relp.heading IS NULL OR p.heading IS NULL)
                                OR (
                                    relp.heading IS NOT NULL
                                    AND p.heading IS NOT NULL
                                    AND ABS(relp.heading - p.heading) <= 100
                                )
                            )
                        ORDER BY relsp.seq_id, p.geom <-> relp.geom
                    ) a
                ) relp ON TRUE
                WHERE sp.seq_id = %(seq)s
                    AND p.id = %(pic)s
                    AND (p.account_id = %(acc)s OR p.status != 'hidden')
                    AND (s.status != 'hidden' OR s.account_id = %(acc)s)
                    AND s.status != 'deleted'
                """,
                {"seq": collectionId, "pic": itemId, "acc": accountId},
            ).fetchone()

            if record is None:
                return None

            return dbPictureToStacItem(collectionId, record)


@bp.route("/collections/<uuid:collectionId>/items/<uuid:itemId>")
def getCollectionItem(collectionId, itemId):
    """Get a single item from a collection
    ---
    tags:
        - Pictures
    parameters:
        - name: collectionId
          in: path
          description: ID of collection to retrieve
          required: true
          schema:
            type: string
        - name: itemId
          in: path
          description: ID of item to retrieve
          required: true
          schema:
            type: string
    responses:
        102:
            description: the item (which is still under process)
            content:
                application/geo+json:
                    schema:
                        $ref: '#/components/schemas/GeoVisioItem'
        200:
            description: the wanted item
            content:
                application/geo+json:
                    schema:
                        $ref: '#/components/schemas/GeoVisioItem'
    """

    stacItem = _getPictureItemById(collectionId, itemId)
    if stacItem is None:
        raise errors.InvalidAPIUsage(_("Item doesn't exist"), status_code=404)

    account = auth.get_current_account()
    picStatusToHttpCode = {
        "waiting-for-process": 102,
        "ready": 200,
        "hidden": 200 if account else 404,
        "broken": 500,
    }
    return stacItem, picStatusToHttpCode[stacItem["properties"]["geovisio:status"]], {"Content-Type": "application/geo+json"}


@bp.route("/search", methods=["GET", "POST"])
def searchItems():
    """Search through all available items

    Note: when searching with a bounding box or a geometry, the items will be sorted by proximity of the center of this bounding box / geometry
    Else the items will not be sorted.
    ---
    tags:
        - Pictures
    get:
        parameters:
            - $ref: '#/components/parameters/STAC_bbox'
            - $ref: '#/components/parameters/STAC_intersects'
            - $ref: '#/components/parameters/STAC_datetime'
            - $ref: '#/components/parameters/STAC_limit'
            - $ref: '#/components/parameters/STAC_ids'
            - $ref: '#/components/parameters/STAC_collectionsArray'
            - $ref: '#/components/parameters/GeoVisio_place_position'
            - $ref: '#/components/parameters/GeoVisio_place_distance'
            - $ref: '#/components/parameters/GeoVisio_place_fov_tolerance'
    post:
        requestBody:
            required: true
            content:
              application/json:
                schema:
                  $ref: '#/components/schemas/GeoVisioItemSearchBody'
    responses:
        200:
            $ref: '#/components/responses/STAC_search'
    """

    account = auth.get_current_account()
    accountId = account.id if account is not None else None
    sqlWhere = [SQL("(p.status = 'ready' OR p.account_id = %(account)s)"), SQL("(is_sequence_visible_by_user(s, %(account)s))")]
    sqlParams: Dict[str, Any] = {"account": accountId}
    sqlSubQueryWhere = [SQL("(p.status = 'ready' OR p.account_id = %(account)s)")]
    order_by = SQL("")

    #
    # Parameters parsing and verification
    #

    # Method + content-type
    args: MultiDict[str, str]
    if request.method == "POST":
        if request.headers.get("Content-Type") != "application/json":
            raise errors.InvalidAPIUsage(_("Search using POST method should have a JSON body"), status_code=400)
        args = MultiDict(request.json)
    else:
        args = request.args

    # Limit
    if args.get("limit") is not None:
        limit = args.get("limit", type=int)
        if limit is None or limit < 1 or limit > 10000:
            raise errors.InvalidAPIUsage(_("Parameter limit must be either empty or a number between 1 and 10000"), status_code=400)
        else:
            sqlParams["limit"] = limit
    else:
        sqlParams["limit"] = 10

    # Bounding box
    bboxarg = parse_bbox(args.getlist("bbox"))
    if bboxarg is not None:
        sqlWhere.append(SQL("p.geom && ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 4326)"))
        sqlParams["minx"] = bboxarg[0]
        sqlParams["miny"] = bboxarg[1]
        sqlParams["maxx"] = bboxarg[2]
        sqlParams["maxy"] = bboxarg[3]
        # if we search by bbox, we'll give first the items near the center of the bounding box
        order_by = SQL("ORDER BY p.geom <-> ST_Centroid(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 4326))")

    # Datetime
    min_dt, max_dt = parse_datetime_interval(args.get("datetime"))
    if min_dt is not None:
        sqlWhere.append(SQL("p.ts >= %(mints)s::timestamp with time zone"))
        sqlParams["mints"] = min_dt

    if max_dt is not None:
        sqlWhere.append(SQL("p.ts <= %(maxts)s::timestamp with time zone"))
        sqlParams["maxts"] = max_dt

    # Place position & distance
    place_pos = parse_lonlat(args.getlist("place_position"), "place_position")
    if place_pos is not None:
        sqlParams["placex"] = place_pos[0]
        sqlParams["placey"] = place_pos[1]

        # Filter to keep pictures in acceptable distance range to POI
        place_dist = parse_distance_range(args.get("place_distance"), "place_distance") or [3, 15]
        sqlParams["placedmin"] = place_dist[0]
        sqlParams["placedmax"] = place_dist[1]

        sqlWhere.append(
            SQL(
                """
                ST_Intersects(
                    p.geom,
                    ST_Difference(
                    ST_Buffer(ST_Point(%(placex)s, %(placey)s)::geography, %(placedmax)s)::geometry,
                        ST_Buffer(ST_Point(%(placex)s, %(placey)s)::geography, %(placedmin)s)::geometry
                    )
                )
                """
            )
        )

        # Compute acceptable field of view
        place_fov_tolerance = args.get("place_fov_tolerance", type=int, default=30)
        if place_fov_tolerance < 2 or place_fov_tolerance > 180:
            raise errors.InvalidAPIUsage(
                _("Parameter place_fov_tolerance must be either empty or a number between 2 and 180"), status_code=400
            )
        else:
            sqlParams["placefov"] = place_fov_tolerance / 2

        sqlWhere.append(
            SQL(
                """(
                p.metadata->>'type' = 'equirectangular'
                OR ST_Azimuth(p.geom, ST_Point(%(placex)s, %(placey)s, 4326)) BETWEEN radians(p.heading - %(placefov)s) AND radians(p.heading + %(placefov)s)
            )"""
            )
        )

        # Sort pictures by nearest to POI
        order_by = SQL("ORDER BY p.geom <-> ST_Point(%(placex)s, %(placey)s, 4326)")

    # Intersects
    if args.get("intersects") is not None:
        try:
            intersects = json.loads(args["intersects"])
        except:
            raise errors.InvalidAPIUsage(_("Parameter intersects should contain a valid GeoJSON Geometry (not a Feature)"), status_code=400)
        if intersects["type"] == "Point":
            sqlWhere.append(SQL("p.geom && ST_Expand(ST_GeomFromGeoJSON(%(geom)s), 0.000001)"))
        else:
            sqlWhere.append(SQL("p.geom && ST_GeomFromGeoJSON(%(geom)s)"))
            sqlWhere.append(SQL("ST_Intersects(p.geom, ST_GeomFromGeoJSON(%(geom)s))"))
        sqlParams["geom"] = Jsonb(intersects)
        # if we search by bbox, we'll give first the items near the center of the bounding box
        order_by = SQL("ORDER BY p.geom <-> ST_Centroid(ST_GeomFromGeoJSON(%(geom)s))")

    # Ids
    if args.get("ids") is not None:
        sqlWhere.append(SQL("p.id = ANY(%(ids)s)"))
        try:
            sqlParams["ids"] = [UUID(j) for j in parse_list(args.get("ids"), paramName="ids")]
        except:
            raise errors.InvalidAPIUsage(_("Parameter ids should be a JSON array of strings"), status_code=400)

    # Collections
    if args.get("collections") is not None:
        sqlWhere.append(SQL("sp.seq_id = ANY(%(collections)s)"))

        # custom subquery filtering to help PG query plan
        sqlSubQueryWhere.append(SQL("sp.seq_id = ANY(%(collections)s)"))

        try:
            sqlParams["collections"] = [UUID(j) for j in parse_list(args["collections"], paramName="collections")]
        except:
            raise errors.InvalidAPIUsage(_("Parameter collections should be a JSON array of strings"), status_code=400)

    # To speed up search, if it's a search by id and on only one id, we use the same code as /collections/:cid/items/:id
    if args.get("ids") is not None and args:
        ids = parse_list(args.get("ids"), paramName="ids")
        if ids and len(ids) == 1:
            picture_id = ids[0]

            with current_app.pool.connection() as conn, conn.cursor() as cursor:
                seq = cursor.execute("SELECT seq_id FROM sequences_pictures WHERE pic_id = %s", [picture_id]).fetchone()
                if not seq:
                    raise errors.InvalidAPIUsage(_("Picture doesn't exist"), status_code=404)

                item = _getPictureItemById(seq[0], UUID(picture_id))
                features = [item] if item else []
                return (
                    {"type": "FeatureCollection", "features": features, "links": [get_root_link()]},
                    200,
                    {"Content-Type": "application/geo+json"},
                )

    #
    # Database query
    #
    with db.cursor(current_app, timeout=30000, row_factory=dict_row) as cursor:
        query = SQL(
            """
SELECT * FROM (
    SELECT
        p.id, p.ts, p.heading, p.metadata, p.inserted_at,
        ST_AsGeoJSON(p.geom)::json AS geojson,
        sp.seq_id, sp.rank AS rank,
        accounts.name AS account_name, 
        p.account_id AS account_id,
        p.exif, p.gps_accuracy_m, p.h_pixel_density,
        t.semantics
    FROM pictures p
    LEFT JOIN sequences_pictures sp ON p.id = sp.pic_id
    LEFT JOIN sequences s ON s.id = sp.seq_id
    LEFT JOIN accounts ON p.account_id = accounts.id
    LEFT JOIN (
        SELECT picture_id, json_agg(json_strip_nulls(json_build_object(
            'key', key,
            'value', value
        ))) AS semantics
        FROM pictures_semantics
        GROUP BY picture_id
    ) t ON t.picture_id = p.id
    WHERE {sqlWhere}
    {orderBy}
    LIMIT %(limit)s
) pic
LEFT JOIN LATERAL (
    SELECT
    p.id AS prevpic, ST_AsGeoJSON(p.geom)::json AS prevpicgeojson
    FROM sequences_pictures sp
    JOIN pictures p ON sp.pic_id = p.id
    WHERE pic.seq_id = sp.seq_id AND {sqlSubQueryWhere} AND sp.rank < pic.rank 
    ORDER BY sp.rank DESC 
    LIMIT 1
) prev on true
LEFT JOIN LATERAL (
    SELECT
    p.id AS nextpic, ST_AsGeoJSON(p.geom)::json AS nextpicgeojson
    FROM sequences_pictures sp
    JOIN pictures p ON sp.pic_id = p.id
    WHERE pic.seq_id = sp.seq_id AND {sqlSubQueryWhere} AND sp.rank > pic.rank 
    ORDER BY sp.rank ASC 
    LIMIT 1
) next on true
;
        """
        ).format(sqlWhere=SQL(" AND ").join(sqlWhere), sqlSubQueryWhere=SQL(" AND ").join(sqlSubQueryWhere), orderBy=order_by)

        records = cursor.execute(query, sqlParams)

        items = [dbPictureToStacItem(str(dbPic["seq_id"]), dbPic) for dbPic in records]

        return (
            {
                "type": "FeatureCollection",
                "features": items,
                "links": [
                    get_root_link(),
                ],
            },
            200,
            {"Content-Type": "application/geo+json"},
        )


# @bp.route("/collections/<uuid:collectionId>/items", methods=["POST"])
# @auth.login_required_by_setting("API_FORCE_AUTH_ON_UPLOAD")
# def postCollectionItem_1(collectionId, account=None):
#     """Add a new picture in a given sequence
#     ---
#     tags:
#         - Upload
#     parameters:
#         - name: collectionId
#           in: path
#           description: ID of sequence to add this picture into
#           required: true
#           schema:
#             type: string
#     requestBody:
#         content:
#             multipart/form-data:
#                 schema:
#                     $ref: '#/components/schemas/GeoVisioPostItem'
#     security:
#         - bearerToken: []
#         - cookieAuth: []
#     responses:
#         202:
#             description: the added picture metadata
#             content:
#                 application/geo+json:
#                     schema:
#                         $ref: '#/components/schemas/GeoVisioItem'
#     """

#     if not request.headers.get("Content-Type", "").startswith("multipart/form-data"):
#         raise errors.InvalidAPIUsage(_("Content type should be multipart/form-data"), status_code=415)

#     # Check if position was given
#     if request.form.get("position") is None:
#         raise errors.InvalidAPIUsage(_('Missing "position" parameter'), status_code=400)
#     else:
#         try:
#             position = int(request.form["position"])
#             if position <= 0:
#                 raise ValueError()
#         except ValueError:
#             raise errors.InvalidAPIUsage(_("Position in sequence should be a positive integer"), status_code=400)

#     # Check if datetime was given
#     ext_mtd = PictureMetadata()
#     if request.form.get("override_capture_time") is not None:
#         ext_mtd.capture_time = parse_datetime(
#             request.form.get("override_capture_time"),
#             error="Parameter `override_capture_time` is not a valid datetime, it should be an iso formated datetime (like '2017-07-21T17:32:28Z').",
#         )

#     # Check if lat/lon were given
#     lon, lat = request.form.get("override_longitude"), request.form.get("override_latitude")
#     if lon is not None or lat is not None:
#         if lat is None:
#             raise errors.InvalidAPIUsage(_("Longitude cannot be overridden alone, override_latitude also needs to be set"))
#         if lon is None:
#             raise errors.InvalidAPIUsage(_("Latitude cannot be overridden alone, override_longitude also needs to be set"))
#         lon = as_longitude(lon, error=_("For parameter `override_longitude`, `%(v)s` is not a valid longitude", v=lon))
#         lat = as_latitude(lat, error=_("For parameter `override_latitude`, `%(v)s` is not a valid latitude", v=lat))
#         ext_mtd.longitude = lon
#         ext_mtd.latitude = lat

#     # Check if others override elements were given
#     override_elmts = {}
#     for k, v in request.form.to_dict().items():
#         if not (k.startswith("override_Exif.") or k.startswith("override_Xmp.")):
#             continue
#         exif_tag = k.replace("override_", "")
#         override_elmts[exif_tag] = v

#     if override_elmts:
#         ext_mtd.additional_exif = override_elmts

#     # Check if picture blurring status is valid
#     if request.form.get("isBlurred") is None or request.form.get("isBlurred") in ["true", "false"]:
#         isBlurred = request.form.get("isBlurred") == "true"
#     else:
#         raise errors.InvalidAPIUsage(_("Picture blur status should be either unset, true or false"), status_code=400)

#     # Check if a picture file was given
#     if "picture" not in request.files:
#         raise errors.InvalidAPIUsage(_("No picture file was sent"), status_code=400)
#     else:
#         picture = request.files["picture"]

#         # Check file validity
#         if not (picture.filename != "" and "." in picture.filename and picture.filename.rsplit(".", 1)[1].lower() in ["jpg", "jpeg"]):
#             raise errors.InvalidAPIUsage(_("Picture file is either missing or in an unsupported format (should be jpg)"), status_code=400)

#     with db.conn(current_app) as conn:
#         with conn.transaction(), conn.cursor() as cursor:
#             # Check if sequence exists
#             seq = cursor.execute("SELECT account_id, status FROM sequences WHERE id = %s", [collectionId]).fetchone()
#             if not seq:
#                 raise errors.InvalidAPIUsage(_("Collection %(s)s wasn't found in database", s=collectionId), status_code=404)

#             # Account associated to picture doesn't match current user
#             if account is not None and account.id != str(seq[0]):
#                 raise errors.InvalidAPIUsage(_("You're not authorized to add picture to this collection"), status_code=403)

#             # Check if sequence has not been deleted
#             status = seq[1]
#             if status == "deleted":
#                 raise errors.InvalidAPIUsage(_("The collection has been deleted, impossible to add pictures to it"), status_code=404)

#             # Compute various metadata
#             accountId = accountIdOrDefault(account)
#             raw_pic = picture.read()
#             filesize = len(raw_pic)

#             with sentry_sdk.start_span(description="computing md5"):
#                 # we save the content hash md5 as uuid since md5 is 128bit and uuid are efficiently handled in postgres
#                 md5 = hashlib.md5(raw_pic).digest()
#                 md5 = UUID(bytes=md5)

#             additionalMetadata = {
#                 "blurredByAuthor": isBlurred,
#                 "originalFileName": os.path.basename(picture.filename),
#                 "originalFileSize": filesize,
#                 "originalContentMd5": md5,
#             }

#             # Update picture metadata if needed
#             with sentry_sdk.start_span(description="overwriting metadata"):
#                 updated_picture = writePictureMetadata(raw_pic, ext_mtd)

#             # Insert picture into database
#             with sentry_sdk.start_span(description="Insert picture in db"):
#                 try:
#                     picId = utils.pictures.insertNewPictureInDatabase(
#                         conn, collectionId, position, updated_picture, accountId, additionalMetadata, lang=get_locale().language
#                     )
#                 except utils.pictures.PicturePositionConflict:
#                     raise errors.InvalidAPIUsage(_("Picture at given position already exist"), status_code=409)
#                 except utils.pictures.MetadataReadingError as e:
#                     raise errors.InvalidAPIUsage(_("Impossible to parse picture metadata"), payload={"details": {"error": e.details}})
#                 except utils.pictures.InvalidMetadataValue as e:
#                     raise errors.InvalidAPIUsage(_("Picture has invalid metadata"), payload={"details": {"error": e.details}})

#             # Save file into appropriate filesystem
#             with sentry_sdk.start_span(description="Saving picture"):
#                 try:
#                     utils.pictures.saveRawPicture(picId, updated_picture, isBlurred)
#                 except:
#                     logging.exception("Picture wasn't correctly saved in filesystem")
#                     raise errors.InvalidAPIUsage(_("Picture wasn't correctly saved in filesystem"), status_code=500)

#     current_app.background_processor.process_pictures()

#     # Return picture metadata
#     return (
#         getCollectionItem(collectionId, picId)[0],
#         202,
#         {
#             "Content-Type": "application/json",
#             "Access-Control-Expose-Headers": "Location",  # Needed for allowing web browsers access Location header
#             "Location": url_for("stac_items.getCollectionItem", _external=True, collectionId=collectionId, itemId=picId),
#         },
#     )


class PatchItemParameter(BaseModel):
    """Parameters used to add an item to an UploadSet"""

    heading: Optional[int] = None
    """Heading of the picture. The new heading will not be persisted in the picture's exif tags for the moment."""
    visible: Optional[bool] = None
    """Should the picture be publicly visible ?"""

    capture_time: Optional[datetime] = None
    """Capture time of the picture. The new capture time will not be persisted in the picture's exif tags for the moment."""
    longitude: Optional[float] = None
    """Longitude of the picture. The new longitude will not be persisted in the picture's exif tags for the moment."""
    latitude: Optional[float] = None
    """Latitude of the picture. The new latitude will not be persisted in the picture's exif tags for the moment."""

    semantics: Optional[List[SemanticTagUpdate]] = None
    """Tags to update on the picture. By default each tag will be added to the picture's tags, but you can change this behavior by setting the `action` parameter to `delete`.

    If you want to replace a tag, you need to first delete it, then add it again.

    Like:
[
    {"key": "some_key", "value": "some_value", "action": "delete"},
    {"key": "some_key", "value": "some_new_value"}
]

    
    Note that updating tags is only possible with JSON data, not with form-data."""

    def has_override(self) -> bool:
        return self.model_fields_set

    @field_validator("heading", mode="before")
    @classmethod
    def parse_heading(cls, value):
        if value is None:
            return None
        return parse_picture_heading(value)

    @field_validator("visible", mode="before")
    @classmethod
    def parse_visible(cls, value):
        if value not in ["true", "false"]:
            raise errors.InvalidAPIUsage(_("Picture visibility parameter (visible) should be either unset, true or false"), status_code=400)
        return value == "true"

    @field_validator("capture_time", mode="before")
    @classmethod
    def parse_capture_time(cls, value):
        if value is None:
            return None
        return parse_datetime(
            value,
            error=_(
                "Parameter `capture_time` is not a valid datetime, it should be an iso formated datetime (like '2017-07-21T17:32:28Z')."
            ),
        )

    @field_validator("longitude")
    @classmethod
    def parse_longitude(cls, value):
        return as_longitude(value, error=_("For parameter `longitude`, `%(v)s` is not a valid longitude", v=value))

    @field_validator("latitude")
    @classmethod
    def parse_latitude(cls, value):
        return as_latitude(value, error=_("For parameter `latitude`, `%(v)s` is not a valid latitude", v=value))

    @model_validator(mode="after")
    def validate(self):
        if self.latitude is None and self.longitude is not None:
            raise errors.InvalidAPIUsage(_("Longitude cannot be overridden alone, latitude also needs to be set"))
        if self.longitude is None and self.latitude is not None:
            raise errors.InvalidAPIUsage(_("Latitude cannot be overridden alone, longitude also needs to be set"))
        return self

    def has_only_semantics_updates(self):
        return self.model_fields_set == {"semantics"}


@bp.route("/collections/<uuid:collectionId>/items/<uuid:itemId>", methods=["PATCH"])
@auth.login_required()
def patchCollectionItem(collectionId, itemId, account):
    """Edits properties of an existing picture

    Note that tags cannot be added as form-data for the moment, only as JSON.

    Note that there are rules on the editing of a picture's metadata:

    - Only the owner of a picture can change its visibility
    - For core metadata (heading, capture_time, position, longitude, latitude), the owner can restrict their change by other accounts (see `collaborative_metadata` field in `/api/users/me`) and if not explicitly defined by the user, the instance's default value is used.
    - Everyone can add/edit/delete semantics tags.
    ---
    tags:
        - Editing
        - Tags
    parameters:
        - name: collectionId
          in: path
          description: ID of sequence the picture belongs to
          required: true
          schema:
            type: string
        - name: itemId
          in: path
          description: ID of picture to edit
          required: true
          schema:
            type: string
    requestBody:
        content:
            application/json:
                schema:
                    $ref: '#/components/schemas/GeoVisioPatchItem'
            application/x-www-form-urlencoded:
                schema:
                    $ref: '#/components/schemas/GeoVisioPatchItem'
            multipart/form-data:
                schema:
                    $ref: '#/components/schemas/GeoVisioPatchItem'
    security:
        - bearerToken: []
        - cookieAuth: []
    responses:
        200:
            description: the wanted item
            content:
                application/geo+json:
                    schema:
                        $ref: '#/components/schemas/GeoVisioItem'
    """

    # Parse received parameters

    metadata = None
    content_type = (request.headers.get("Content-Type") or "").split(";")[0]

    try:
        if request.is_json and request.json:
            metadata = PatchItemParameter(**request.json)
        elif content_type in ["multipart/form-data", "application/x-www-form-urlencoded"]:
            metadata = PatchItemParameter(**request.form)
    except ValidationError as ve:
        raise errors.InvalidAPIUsage(_("Impossible to parse parameters"), payload=validation_error(ve))

    # If no parameter is set
    if metadata is None or not metadata.has_override():
        return getCollectionItem(collectionId, itemId)

    # Check if picture exists and if given account is authorized to edit
    with db.conn(current_app) as conn:
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
            pic = cursor.execute("SELECT status, account_id FROM pictures WHERE id = %s", [itemId]).fetchone()

            # Picture not found
            if not pic:
                raise errors.InvalidAPIUsage(_("Picture %(p)s wasn't found in database", p=itemId), status_code=404)

            if account is not None and account.id != str(pic["account_id"]):
                # Account associated to picture doesn't match current user
                # and we limit the status change to only the owner.
                if metadata.visible is not None:
                    raise errors.InvalidAPIUsage(
                        _("You're not authorized to edit the visibility of this picture. Only the owner can change this."), status_code=403
                    )

                # for core metadata editing (all appart the semantic tags), we check if the user has allowed it
                if not metadata.has_only_semantics_updates():
                    if not auth.account_allow_collaborative_editing(pic["account_id"]):
                        raise errors.InvalidAPIUsage(
                            _("You're not authorized to edit this picture, collaborative editing is not allowed"),
                            status_code=403,
                        )
            sqlUpdates = []
            sqlParams = {"id": itemId, "account": account.id}

            # Let's edit this picture
            oldStatus = pic["status"]
            if oldStatus not in ["ready", "hidden"]:
                # Picture is in a preparing/broken/... state so no edit possible
                raise errors.InvalidAPIUsage(
                    _(
                        "Picture %(p)s is in %(s)s state, its visibility can't be changed for now",
                        p=itemId,
                        s=oldStatus,
                    ),
                    status_code=400,
                )

            newStatus = None
            if metadata.visible is not None:
                newStatus = "ready" if metadata.visible is True else "hidden"
                if newStatus != oldStatus:
                    sqlUpdates.append(SQL("status = %(status)s"))
                    sqlParams["status"] = newStatus

            if metadata.heading is not None:
                sqlUpdates.extend([SQL("heading = %(heading)s"), SQL("heading_computed = false")])
                sqlParams["heading"] = metadata.heading

            if metadata.capture_time is not None:
                sqlUpdates.extend([SQL("ts = %(capture_time)s")])
                sqlParams["capture_time"] = metadata.capture_time

            if metadata.longitude is not None and metadata.latitude is not None:
                sqlUpdates.extend([SQL("geom = ST_SetSRID(ST_MakePoint(%(longitude)s, %(latitude)s), 4326)")])
                sqlParams["longitude"] = metadata.longitude
                sqlParams["latitude"] = metadata.latitude

            if metadata.semantics is not None:
                # semantic tags are managed separately
                update_tags(cursor, Entity(type=EntityType.pic, id=itemId), metadata.semantics, account=account.id)

            if sqlUpdates:
                # Note: we set the field `last_account_to_edit` to track who changed the collection last
                # setting this field will trigger the history tracking of the collection (using postgres trigger)
                sqlUpdates.append(SQL("last_account_to_edit = %(account)s"))

                cursor.execute(
                    SQL(
                        """UPDATE pictures
SET {updates}
WHERE id = %(id)s"""
                    ).format(updates=SQL(", ").join(sqlUpdates)),
                    sqlParams,
                )

    # Redirect response to a classic GET
    return getCollectionItem(collectionId, itemId)


@bp.route("/collections/<uuid:collectionId>/items/<uuid:itemId>", methods=["DELETE"])
@auth.login_required()
def deleteCollectionItem(collectionId, itemId, account):
    """Delete an existing picture
    ---
    tags:
        - Editing
    parameters:
        - name: collectionId
          in: path
          description: ID of sequence the picture belongs to
          required: true
          schema:
            type: string
        - name: itemId
          in: path
          description: ID of picture to edit
          required: true
          schema:
            type: string
    security:
        - bearerToken: []
        - cookieAuth: []
    responses:
        204:
            description: The object has been correctly deleted
    """

    # Check if picture exists and if given account is authorized to edit
    with db.conn(current_app) as conn:
        with conn.transaction(), conn.cursor() as cursor:
            pic = cursor.execute("SELECT status, account_id FROM pictures WHERE id = %s", [itemId]).fetchone()

            # Picture not found
            if not pic:
                raise errors.InvalidAPIUsage(_("Picture %(p)s wasn't found in database", p=itemId), status_code=404)

            # Account associated to picture doesn't match current user
            if account is not None and account.id != str(pic[1]):
                raise errors.InvalidAPIUsage(_("You're not authorized to edit this picture"), status_code=403)

            cursor.execute("DELETE FROM pictures WHERE id = %s", [itemId])

    # let the picture be removed from the filesystem by the asynchronous workers
    current_app.background_processor.process_pictures()

    return "", 204


def _getHDJpgPictureURL(picId: str, status: Optional[str]):
    external_url = utils.pictures.getPublicHDPictureExternalUrl(picId, format="jpg")
    if external_url and status == "ready":  # we always serve non ready pictures through the API to be able to check permission:
        return external_url
    return url_for("pictures.getPictureHD", _external=True, pictureId=picId, format="jpg")


def _getSDJpgPictureURL(picId: str, status: Optional[str]):
    external_url = utils.pictures.getPublicDerivatePictureExternalUrl(picId, format="jpg", derivateFileName="sd.jpg")
    if external_url and status == "ready":  # we always serve non ready pictures through the API to be able to check permission:
        return external_url
    return url_for("pictures.getPictureSD", _external=True, pictureId=picId, format="jpg")


def _getThumbJpgPictureURL(picId: str, status: Optional[str]):
    external_url = utils.pictures.getPublicDerivatePictureExternalUrl(picId, format="jpg", derivateFileName="thumb.jpg")
    if external_url and status == "ready":  # we always serve non ready pictures through the API to be able to check permission
        return external_url
    return url_for("pictures.getPictureThumb", _external=True, pictureId=picId, format="jpg")


def _getTilesJpgPictureURL(picId: str, status: Optional[str]):
    external_url = utils.pictures.getPublicDerivatePictureExternalUrl(picId, format="jpg", derivateFileName="tiles/{TileCol}_{TileRow}.jpg")
    if external_url and status == "ready":  # we always serve non ready pictures through the API to be able to check permission:
        return external_url
    return unquote(url_for("pictures.getPictureTile", _external=True, pictureId=picId, format="jpg", col="{TileCol}", row="{TileRow}"))
    
    
    
    
    
from flask import request, url_for, current_app
from flask_babel import _
from uuid import UUID
import os
import hashlib
import pandas as pd
import psycopg
# from your_app import errors, utils
# from your_app.metadata import PictureMetadata, parse_datetime, as_longitude, as_latitude
from gettext import gettext as translate



# @bp.route("/collections/<uuid:collectionId>/items", methods=["POST"])
# @auth.login_required_by_setting("API_FORCE_AUTH_ON_UPLOAD")
# def postCollectionItem(collectionId, account=None):
#     """
#     Add a new picture in a given sequence
#     """
#     import os
#     import pandas as pd

#     if not request.headers.get("Content-Type", "").startswith("multipart/form-data"):
#         raise errors.InvalidAPIUsage("Content type should be multipart/form-data", status_code=415)

#     if "excel" not in request.files:
#         raise errors.InvalidAPIUsage("No Excel file was sent", status_code=400)

#     excel_file = request.files["excel"]
#     try:
#         df = pd.read_excel(excel_file)
#     except Exception as e:
#         raise errors.InvalidAPIUsage(f"Error reading Excel file: {str(e)}", status_code=400)

#     required_columns = {'position', 'picture', 'override_capture_time', 'override_longitude', 'override_latitude'}
#     if not required_columns.issubset(df.columns):
#         raise errors.InvalidAPIUsage(f"Missing required columns: {required_columns - set(df.columns)}", status_code=400)

#     processed_pictures = []

#     for _, row in df.iterrows():
#         position = row['position']
#         picture_path = row['picture']

#         if not os.path.exists(picture_path):
#             raise errors.InvalidAPIUsage(f"Picture file {picture_path} not found on the server.", status_code=400)

#         with open(picture_path, "rb") as f:
#             raw_pic = f.read()

#         # Validate position
#         try:
#             position = int(position)
#             if position <= 0:
#                 raise ValueError()
#         except ValueError:
#             raise errors.InvalidAPIUsage("Position should be a positive integer", status_code=400)
#         # Check if picture blurring status is valid
#         isBlurred = False
#         # Capture additional metadata if provided
#         ext_mtd = PictureMetadata()
#         if pd.notna(row.get('override_capture_time')):
#             try:
#                 # Parse the datetime
#                 ext_mtd.capture_time = parse_datetime(
#                     row['override_capture_time'],
#                     error="Parameter `override_capture_time` is not a valid datetime, it should be an ISO formatted datetime (like '2017-07-21T17:32:28Z')."
#                 )

#                 # Check if timezone is valid
#                 if ext_mtd.capture_time.tzinfo:
#                     offset_minutes = ext_mtd.capture_time.utcoffset().total_seconds() / 60
#                     if not (-12 * 60 <= offset_minutes <= 14 * 60):  # Valid range: -720 to 840 minutes
#                         print(f"Invalid timezone detected: {offset_minutes / 60:+} hours. Removing timezone info.")
#                         ext_mtd.capture_time = ext_mtd.capture_time.replace(tzinfo=None)  # Remove invalid timezone

#             except Exception as e:
#                 print(f"Error parsing datetime: {e}")

#         if pd.notna(row.get('override_longitude')) and pd.notna(row.get('override_latitude')):
#             lon = as_longitude(row['override_longitude'], error=translate("For parameter `override_longitude` is not a valid longitude"))
#             lat = as_latitude(row['override_latitude'], error=translate("For parameter `override_latitude` is not a valid latitude"))

#             ext_mtd.longitude = lon
#             ext_mtd.latitude = lat

#         # Compute MD5 hash
#         md5 = hashlib.md5(raw_pic).digest()
#         md5 = UUID(bytes=md5)

#         # Compute various metadata
#         accountId = accountIdOrDefault(account)
#         ext_mtd.isBlurred='false'
#         additionalMetadata = {
#             "originalFileName": os.path.basename(picture_path),
#             "originalFileSize": len(raw_pic),
#             "originalContentMd5": md5,
#         }

#         processed_pictures.append((position, raw_pic, ext_mtd, additionalMetadata))

#     # Process and save pictures
#     with db.conn(current_app) as conn:
#         with conn.transaction(), conn.cursor() as cursor:
#             for position, raw_pic, ext_mtd, additionalMetadata in processed_pictures:
                
#                     updated_picture = writePictureMetadata(raw_pic, ext_mtd)
#                    # print("**********updated_picture**********",updated_picture)
#                     picId = utils.pictures.insertNewPictureInDatabase(
#                         conn, collectionId, position, updated_picture, accountId, additionalMetadata
#                     )
                  
#                     utils.pictures.saveRawPicture(picId, updated_picture,isBlurred='false')

                

#     current_app.background_processor.process_pictures()

#     return ("Pictures processed successfully", 202)



# @bp.route("/collections/<uuid:collectionId>/items", methods=["POST"])
# @auth.login_required_by_setting("API_FORCE_AUTH_ON_UPLOAD")
# def postCollectionItem(collectionId, account=None):
#     """
#     Add a new picture in a given sequence
#     """
#     import os
#     import pandas as pd
#     from datetime import timezone

#     if not request.headers.get("Content-Type", "").startswith("multipart/form-data"):
#         raise errors.InvalidAPIUsage("Content type should be multipart/form-data", status_code=415)

#     if "excel" not in request.files:
#         raise errors.InvalidAPIUsage("No Excel file was sent", status_code=400)

#     excel_file = request.files["excel"]
#     print("*************************excel_file****************************",excel_file)
#     try:
#         df = pd.read_excel(excel_file)
#     except Exception as e:
#         raise errors.InvalidAPIUsage(f"Error reading Excel file: {str(e)}", status_code=400)

#     required_columns = {'picture', 'override_longitude', 'override_latitude'}
#     if not required_columns.issubset(df.columns):
#         raise errors.InvalidAPIUsage(f"Missing required columns: {required_columns - set(df.columns)}", status_code=400)

#     processed_pictures = []

#     # Start position from 1
#     current_position = 1
#     for _, row in df.iterrows():
#         #position = row['position']
#         picture_path = row['picture']

#          # Assign and increment position
#         position = current_position
#         print("*****************************************************",position)
#         current_position += 1
#         if not os.path.exists(picture_path):
#             raise errors.InvalidAPIUsage(f"Picture file {picture_path} not found on the server.", status_code=400)

#         with open(picture_path, "rb") as f:
#             raw_pic = f.read()

#         # Validate position
#         # try:
#         #     position = int(position)
#         #     if position <= 0:
#         #         raise ValueError()
#         # except ValueError:
#         #     raise errors.InvalidAPIUsage("Position should be a positive integer", status_code=400)
#         # Check if picture blurring status is valid
#         isBlurred = False
#         #default_capture_time = datetime.now(timezone.utc).isoformat()
#         # Capture additional metadata if provided
#         ext_mtd = PictureMetadata()
#         ext_mtd.capture_time = datetime.now(timezone.utc)
#         # Try assigning capture time from Excel if provided (optional)
#         # if pd.notna(row.get("override_capture_time")):
#         #     ext_mtd.capture_time = parse_datetime(
#         #         row["override_capture_time"],
#         #         error="Parameter `override_capture_time` is not a valid datetime, it should be an ISO formatted datetime (like '2017-07-21T17:32:28Z').",
#         #     )
#         # else:
#         #     ext_mtd.capture_time = datetime.now(timezone.utc).isoformat()

#         if pd.notna(row.get('override_longitude')) and pd.notna(row.get('override_latitude')):
#             lon = as_longitude(row['override_longitude'], error=translate("For parameter `override_longitude` is not a valid longitude"))
#             lat = as_latitude(row['override_latitude'], error=translate("For parameter `override_latitude` is not a valid latitude"))

#             ext_mtd.longitude = lon
#             ext_mtd.latitude = lat

#         # Compute MD5 hash
#         md5 = hashlib.md5(raw_pic).digest()
#         md5 = UUID(bytes=md5)

#         # Compute various metadata
#         accountId = accountIdOrDefault(account)
#         ext_mtd.isBlurred='false'
#         additionalMetadata = {
#             "originalFileName": os.path.basename(picture_path),
#             "originalFileSize": len(raw_pic),
#             "originalContentMd5": md5,
#         }

#         processed_pictures.append((position, raw_pic, ext_mtd, additionalMetadata))

#     # Process and save pictures
#     with db.conn(current_app) as conn:
#         with conn.transaction(), conn.cursor() as cursor:
#             for position, raw_pic, ext_mtd, additionalMetadata in processed_pictures:
                
#                     updated_picture = writePictureMetadata(raw_pic, ext_mtd)
#                    # print("**********updated_picture**********",updated_picture)
#                     picId = utils.pictures.insertNewPictureInDatabase(
#                         conn, collectionId, position, updated_picture, accountId, additionalMetadata
#                     )
                  
#                     utils.pictures.saveRawPicture(picId, updated_picture,isBlurred='false')

                

#     current_app.background_processor.process_pictures()

#     return ("Pictures processed successfully", 202)



######################################################################################
####################################################################################
#####################################################################################
#####################################################################################
########################################################################################
'''
# from flask import request
@bp.route("/collections/<uuid:collectionId>/items", methods=["POST"])
@auth.login_required_by_setting("API_FORCE_AUTH_ON_UPLOAD")
def postCollectionItem(collectionId, account=None):
    """
    Add a new picture in a given sequence
    """
    import os
    import pandas as pd
    from datetime import timezone
   

    # if not request.headers.get("Content-Type", "").startswith("multipart/form-data"):
    #     raise errors.InvalidAPIUsage("Content type should be multipart/form-data", status_code=415)
    if not request.is_json:
        raise errors.InvalidAPIUsage("Request must be JSON", status_code=415)
    excel_path = request.json.get("excel_path")
    print("******************excel_path******************",excel_path)
    if not excel_path or not os.path.exists(excel_path):
        raise errors.InvalidAPIUsage("Excel file path not provided or file does not exist", status_code=400)

    try:
        df = pd.read_excel(excel_path)
    except Exception as e:
        raise errors.InvalidAPIUsage(f"Error reading Excel file: {str(e)}", status_code=400)


    required_columns = {'picture', 'override_longitude', 'override_latitude'}
    print("******************required_columns******************",required_columns)
    if not required_columns.issubset(df.columns):
        raise errors.InvalidAPIUsage(f"Missing required columns: {required_columns - set(df.columns)}", status_code=400)

    processed_pictures = []

   
   # Get the current last position in the collection
 
    for _, row in df.iterrows():
        position = row['position']
        picture_path = row['picture']
        picture_path = row['picture']

        # position = current_position
        # current_position += 1  # Increment for next row
        #  # Assign and increment position
        # position = current_position
        # print("*****************************************************",position)
        # current_position += 1
        # if not os.path.exists(picture_path):
        #     raise errors.InvalidAPIUsage(f"Picture file {picture_path} not found on the server.", status_code=400)

        with open(picture_path, "rb") as f:
            raw_pic = f.read()

        # Validate position
        # try:
        #     position = int(position)
        #     if position <= 0:
        #         raise ValueError()
        # except ValueError:
        #     raise errors.InvalidAPIUsage("Position should be a positive integer", status_code=400)
        # Check if picture blurring status is valid
        isBlurred = False
        #default_capture_time = datetime.now(timezone.utc).isoformat()
        # Capture additional metadata if provided
        ext_mtd = PictureMetadata()
      #  ext_mtd.capture_time = datetime.now(timezone.utc)
        #Try assigning capture time from Excel if provided (optional)
        if pd.notna(row.get("override_capture_time")):
            ext_mtd.capture_time = parse_datetime(
                row["override_capture_time"],
                error="Parameter `override_capture_time` is not a valid datetime, it should be an ISO formatted datetime (like '2017-07-21T17:32:28Z').",
            )
        else:
            ext_mtd.capture_time = datetime.now(timezone.utc).isoformat()

        if pd.notna(row.get('override_longitude')) and pd.notna(row.get('override_latitude')):
            lon = as_longitude(row['override_longitude'], error=translate("For parameter `override_longitude` is not a valid longitude"))
            lat = as_latitude(row['override_latitude'], error=translate("For parameter `override_latitude` is not a valid latitude"))

            ext_mtd.longitude = lon
            ext_mtd.latitude = lat

        # Compute MD5 hash
        md5 = hashlib.md5(raw_pic).digest()
        md5 = UUID(bytes=md5)

        # Compute various metadata
        accountId = accountIdOrDefault(account)
        ext_mtd.isBlurred='false'
        additionalMetadata = {
            "originalFileName": os.path.basename(picture_path),
            "originalFileSize": len(raw_pic),
            "originalContentMd5": md5,
        }

        processed_pictures.append((position, raw_pic, ext_mtd, additionalMetadata))

    # Process and save pictures
    with db.conn(current_app) as conn:
        with conn.transaction(), conn.cursor() as cursor:
            for position, raw_pic, ext_mtd, additionalMetadata in processed_pictures:
                
                    updated_picture = writePictureMetadata(raw_pic, ext_mtd)
                   # print("**********updated_picture**********",updated_picture)
                    picId = utils.pictures.insertNewPictureInDatabase(
                        conn, collectionId, position, updated_picture, accountId, additionalMetadata
                    )
                  
                    utils.pictures.saveRawPicture(picId, updated_picture,isBlurred='false')

                

    current_app.background_processor.process_pictures()

    return ("Pictures processed successfully", 202)
'''

# currently working commented by 10 june morning 10:45  

# @bp.route("/collections/<uuid:collectionId>/items", methods=["POST"])
# @auth.login_required_by_setting("API_FORCE_AUTH_ON_UPLOAD")
# def postCollectionItem(collectionId, account=None):
#     """
#     Upload pictures from Excel where each row has a file path.
#     Supports large-scale uploads (50k+).
#     """
#     import os
#     import pandas as pd
#     import hashlib
#     from datetime import datetime, timezone
#     from uuid import UUID
#     import json
#     from flask import Response
#     import time

#     if not request.is_json:
#         raise errors.InvalidAPIUsage("Request must be JSON", status_code=415)

#     excel_path = request.json.get("excel_path")
#     print("******************excel_path******************",excel_path)
#     if not excel_path or not os.path.exists(excel_path):
#         raise errors.InvalidAPIUsage("Excel file path not provided or file does not exist", status_code=400)

#     try:
#         df = pd.read_excel(excel_path)
#     except Exception as e:
#         raise errors.InvalidAPIUsage(f"Error reading Excel file: {str(e)}", status_code=400)

#     required_columns = {'picture', 'override_longitude', 'override_latitude'}
#     print("******************required_columns******************",required_columns)
#     if not required_columns.issubset(df.columns):
#         raise errors.InvalidAPIUsage(f"Missing required columns: {required_columns - set(df.columns)}", status_code=400)

#     if 'is_processed' not in df.columns:
#         df['is_processed'] = False

#     processed_pictures = []

#     accountId = accountIdOrDefault(account)
#     batch_size = 100
#     success_count = 0
#     error_count = 0
#     start_time = time.time()
#     total = len(df)
#     # for _, row in df.iterrows():
#     #     position = row.get('position')
#     #     picture_path = row.get('picture')
#     #     position = row.get('position')

#     #     if not picture_path or not os.path.exists(picture_path):
#     #         raise errors.InvalidAPIUsage(f"Picture file {picture_path} not found.", status_code=400)
        
#     #      # Compute MD5 from disk
#     #     with open(picture_path, "rb") as f:
#     #         raw_pic = f.read()

#     for start in range(0, total, batch_size):
#         end = min(start + batch_size, total)
#         batch_df = df.iloc[start:end]

#         with db.conn(current_app) as conn:
#             with conn.transaction(), conn.cursor() as cursor:
#                 for idx in batch_df.index:
#                     row = df.loc[idx]

#                     if row['is_processed'] == True:
#                         continue

#                     picture_path = row.get('picture')
#                     position = row.get('position')
#                     try:
#                         position = int(position) if pd.notna(position) else None
#                     except Exception as e:
#                         print(f"⚠️ Skipping row due to invalid position: {e}")
#                         error_count += 1
#                         continue

#                     if not picture_path or not os.path.exists(picture_path):
#                         print(f"❌ File not found: {picture_path}")
#                         error_count += 1
#                         continue

#                     try:
#                         with open(picture_path, "rb") as f:
#                             raw_pic = f.read()
           
#                         isBlurred = False
#                         # Metadata
#                         ext_mtd = PictureMetadata()
#                         #Try assigning capture time from Excel if provided (optional)
#                         if pd.notna(row.get("override_capture_time")):
#                             ext_mtd.capture_time = parse_datetime(
#                                 row["override_capture_time"],
#                                 error="Parameter `override_capture_time` is not a valid datetime, it should be an ISO formatted datetime (like '2017-07-21T17:32:28Z').",
#                             )
#                         else:
#                             ext_mtd.capture_time = datetime.now(timezone.utc).isoformat()

#                         if pd.notna(row.get('override_longitude')) and pd.notna(row.get('override_latitude')):
#                             lon = as_longitude(row['override_longitude'], error=translate("For parameter `override_longitude` is not a valid longitude"))
#                             lat = as_latitude(row['override_latitude'], error=translate("For parameter `override_latitude` is not a valid latitude"))

#                             ext_mtd.longitude = lon
#                             ext_mtd.latitude = lat

#                         md5 = UUID(bytes=hashlib.md5(raw_pic).digest())
#                     #  md5 = UUID(bytes=md5)

#                         accountId = accountIdOrDefault(account)
#                         additionalMetadata = {
#                             "originalFileName": os.path.basename(picture_path),
#                             "originalFileSize": len(raw_pic),
#                             "originalContentMd5": md5,
#                         }
#                         updated_picture = writePictureMetadata(raw_pic, ext_mtd)

#                         picId = utils.pictures.insertNewPictureInDatabase(
#                             conn, collectionId, position, updated_picture, accountId, additionalMetadata
#                         )

#                         utils.pictures.saveRawPictureFromPath(picId, picture_path, isBlurred=False)
#                         df.at[idx, 'is_processed'] = True
#                         success_count += 1

#                     except Exception as e:
#                         print(f"❌ Failed to process image at {picture_path}: {e}")
#                         error_count += 1

#         # Save back progress to Excel
#         df.to_excel(excel_path, index=False)

#          # ⏱️ Log every minute
#         if time.time() - start_time >= 60:
#             remaining = len(df[df['is_processed'] == False])
#             print(f"[{round(time.time() - start_time, 2)}s] ✅ Processed: {success_count}, ❌ Errors: {error_count}, ������ Remaining: {remaining}")
#             start_time = time.time()
#     current_app.background_processor.process_pictures()

#     response_data = {
#         "status": "success",
#         "total": total,
#         "processed": success_count,
#         "errors": error_count,
#         "message": f"{success_count} pictures processed, {error_count} failed."
#     }
#     return Response(json.dumps(response_data), status=201, mimetype='application/json')

    # # Save all pictures
    # with db.conn(current_app) as conn:
    #     with conn.transaction(), conn.cursor() as cursor:
    #         for position, picture_path, raw_pic, ext_mtd, additionalMetadata in processed_pictures:
    #             updated_picture = writePictureMetadata(raw_pic, ext_mtd)

    #             picId = utils.pictures.insertNewPictureInDatabase(
    #                 conn, collectionId, position, updated_picture, accountId, additionalMetadata
    #             )

    #             utils.pictures.saveRawPictureFromPath(picId, picture_path, isBlurred=False)

    # current_app.background_processor.process_pictures()
    # response_data = {"status": "success", "message": f"{len(processed_pictures)} pictures processed."}
    # return Response(json.dumps(response_data), status=201, mimetype='application/json')



@bp.route("/collections/<uuid:collectionId>/items", methods=["POST"])
@auth.login_required_by_setting("API_FORCE_AUTH_ON_UPLOAD")
def postCollectionItem(collectionId, account=None):
    """
    Upload pictures from Excel where each row has a file path.
    Supports large-scale uploads (50k+).
    """
    import os
    import pandas as pd
    import hashlib
    from datetime import datetime, timezone
    from uuid import UUID
    import json
    from flask import Response
    import time

    if not request.is_json:
        raise errors.InvalidAPIUsage("Request must be JSON", status_code=415)

    excel_path = request.json.get("excel_path")
    if not excel_path or not os.path.exists(excel_path):
        raise errors.InvalidAPIUsage("Excel file path not provided or file does not exist", status_code=400)

    try:
        df = pd.read_excel(excel_path)
    except Exception as e:
        raise errors.InvalidAPIUsage(f"Error reading Excel file: {str(e)}", status_code=400)

    required_columns = {'picture', 'override_longitude', 'override_latitude'}
    if not required_columns.issubset(df.columns):
        raise errors.InvalidAPIUsage(f"Missing required columns: {required_columns - set(df.columns)}", status_code=400)

    if 'is_processed' not in df.columns:
        df['is_processed'] = False

    # Ensure original TRUEs are preserved later
    original_is_processed = df['is_processed'].copy()

    accountId = accountIdOrDefault(account)
    batch_size = 150
    success_count = 0
    error_count = 0
    start_time = time.time()
    total = len(df)

    # Only process unprocessed rows
    unprocessed_indices = df[df['is_processed'] == False].index

    for start in range(0, len(unprocessed_indices), batch_size):
        end = min(start + batch_size, len(unprocessed_indices))
        batch_indices = unprocessed_indices[start:end]

        with db.conn(current_app) as conn:
            with conn.transaction(), conn.cursor() as cursor:
                for idx in batch_indices:
                    row = df.loc[idx]
                    picture_path = row.get('picture')
                    position = row.get('position')

                    try:
                        position = int(position) if pd.notna(position) else None
                    except Exception as e:
                        print(f"⚠️ Skipping row due to invalid position: {e}")
                        error_count += 1
                        continue

                    if not picture_path or not os.path.exists(picture_path):
                        print(f"❌ File not found: {picture_path}")
                        error_count += 1
                        continue

                  
                    with open(picture_path, "rb") as f:
                            raw_pic = f.read()

                    isBlurred = False
                    ext_mtd = PictureMetadata()

                    if pd.notna(row.get("override_capture_time")):
                            ext_mtd.capture_time = parse_datetime(
                                row["override_capture_time"],
                                error="Parameter `override_capture_time` is not a valid datetime, it should be ISO formatted.",
                            )
                    else:
                            ext_mtd.capture_time = datetime.now(timezone.utc).isoformat()

                    if pd.notna(row.get('override_longitude')) and pd.notna(row.get('override_latitude')):
                            ext_mtd.longitude = as_longitude(row['override_longitude'], error="Invalid longitude")
                            ext_mtd.latitude = as_latitude(row['override_latitude'], error="Invalid latitude")

                    md5 = UUID(bytes=hashlib.md5(raw_pic).digest())

                    additionalMetadata = {
                            "originalFileName": os.path.basename(picture_path),
                            "originalFileSize": len(raw_pic),
                            "originalContentMd5": md5,
                        }

                    updated_picture = writePictureMetadata(raw_pic, ext_mtd)

                    picId = utils.pictures.insertNewPictureInDatabase(
                            conn, collectionId, position, updated_picture, accountId, additionalMetadata
                        )

                    utils.pictures.saveRawPictureFromPath(picId, picture_path, isBlurred=False)

                    df.at[idx, 'is_processed'] = True
                    success_count += 1

                    

        # Log progress every 0.1 sec (for testing)
        if time.time() - start_time >= 60:
            remaining = len(df[df['is_processed'] == False])
            print(f"[{round(time.time() - start_time, 2)}s] ✅ Processed: {success_count}, ❌ Errors: {error_count}, ������ Remaining: {remaining}")
            start_time = time.time()

    # Safely restore any originally TRUE values (in case anything was accidentally reset)
    df['is_processed'] = df['is_processed'].astype(bool) | original_is_processed.astype(bool)


    # Save back to Excel
    df.to_excel(excel_path, index=False)

    current_app.background_processor.process_pictures()

    response_data = {
        "status": "success",
        "total": total,
        "processed": success_count,
        "errors": error_count,
        "message": f"{success_count} pictures processed, {error_count} failed."
    }
    return Response(json.dumps(response_data), status=201, mimetype='application/json')

