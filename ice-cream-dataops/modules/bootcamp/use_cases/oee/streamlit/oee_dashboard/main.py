"""OEE Dashboard

Shows the OEE time series written by the `oee_timeseries` function
(space `oee_ts_space`, external IDs `<equipment>:<metric>`).
Equipment is mapped to sites through the CogniteAsset hierarchy in `icapi_dm_space`,
the same way the function does it.
"""

from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from cognite.client import CogniteClient
from cognite.client.data_classes.data_modeling import NodeId
from cognite.client.data_classes.data_modeling.cdm.v1 import CogniteAsset, CogniteTimeSeries
from cognite.client.data_classes.filters import Prefix

OEE_SPACE = "oee_ts_space"
SOURCE_SPACE = "icapi_dm_space"

SITES = [
    "Houston",
    "Oslo",
    "Kuala_Lumpur",
    "Hannover",
    "Nuremberg",
    "Marseille",
    "Sao_Paulo",
    "Chicago",
    "Rotterdam",
    "London",
]

RATIO_METRICS = ["oee", "availability", "performance", "quality"]
ALL_METRICS = RATIO_METRICS + ["off_spec"]

LOOKBACK_OPTIONS = {
    "Last 1 hour": (60, "1m"),
    "Last 6 hours": (6 * 60, "5m"),
    "Last 24 hours": (24 * 60, "15m"),
    "Last 3 days": (3 * 24 * 60, "1h"),
    "Last 7 days": (7 * 24 * 60, "2h"),
}

st.set_page_config(page_title="OEE Dashboard", layout="wide")


@st.cache_resource
def get_client() -> CogniteClient:
    return CogniteClient()


client = get_client()


@st.cache_data(ttl=600, show_spinner="Loading OEE time series...")
def load_equipment() -> dict[str, list[str]]:
    """Return {equipment_id: [metric, ...]} for all OEE time series."""
    nodes = client.data_modeling.instances.list(
        instance_type=CogniteTimeSeries, space=OEE_SPACE, limit=None
    )
    equipment: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        if ":" not in node.external_id:
            continue
        asset, metric = node.external_id.rsplit(":", 1)
        equipment[asset].append(metric)
    return dict(equipment)


@st.cache_data(ttl=3600, show_spinner="Mapping equipment to sites...")
def load_site_map() -> dict[str, str]:
    """Return {asset_external_id: site} using the asset hierarchy under each site root."""
    site_map: dict[str, str] = {}
    for site in SITES:
        root = client.data_modeling.instances.retrieve_nodes(
            NodeId(SOURCE_SPACE, site.lower()), node_cls=CogniteAsset
        )
        if not root or not root.path:
            continue
        assets = client.data_modeling.instances.list(
            instance_type=CogniteAsset,
            filter=Prefix(property=["cdf_cdm", "CogniteAsset/v1", "path"], value=root.path),
            limit=None,
        )
        for asset in assets:
            site_map[asset.external_id] = site
    return site_map


@st.cache_data(ttl=60, show_spinner="Loading latest values...")
def load_latest(equipment_ids: tuple[str, ...]) -> pd.DataFrame:
    ids = [NodeId(OEE_SPACE, f"{eq}:{m}") for eq in equipment_ids for m in ALL_METRICS]
    latest = client.time_series.data.retrieve_latest(instance_id=ids, ignore_unknown_ids=True)
    rows: dict[str, dict] = defaultdict(dict)
    for dps in latest:
        if not dps.timestamp:
            continue
        asset, metric = dps.instance_id.external_id.rsplit(":", 1)
        rows[asset][metric] = dps.value[0]
        ts = datetime.fromtimestamp(dps.timestamp[0] / 1000, tz=timezone.utc)
        rows[asset]["updated"] = max(rows[asset].get("updated", ts), ts)
    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index.name = "equipment"
    return df


@st.cache_data(ttl=60, show_spinner="Loading history...")
def load_history(equipment_id: str, lookback_minutes: int, granularity: str) -> pd.DataFrame:
    ids = [NodeId(OEE_SPACE, f"{equipment_id}:{m}") for m in ALL_METRICS]
    latest = client.time_series.data.retrieve_latest(instance_id=ids, ignore_unknown_ids=True)
    end_candidates = [dps.timestamp[0] for dps in latest if dps.timestamp]
    if not end_candidates:
        return pd.DataFrame()
    # Anchor on the newest datapoint rather than "now", in case the function is lagging.
    end = max(end_candidates) + 1
    start = end - lookback_minutes * 60 * 1000

    result = client.time_series.data.retrieve(
        instance_id=ids,
        start=start,
        end=end,
        aggregates=["average"],
        granularity=granularity,
        ignore_unknown_ids=True,
        limit=None,
    )
    series = {}
    for dps in result:
        metric = dps.instance_id.external_id.rsplit(":", 1)[1]
        if dps.timestamp:
            series[metric] = pd.Series(
                dps.average, index=pd.to_datetime(dps.timestamp, unit="ms", utc=True)
            )
    return pd.DataFrame(series)


def pct(value) -> str:
    return "–" if pd.isna(value) else f"{value * 100:.1f}%"


# ---------------------------------------------------------------- UI

st.title("🍦 OEE Dashboard")
st.caption(
    "Overall Equipment Effectiveness = Availability × Performance × Quality, "
    "calculated by the `oee_timeseries` function."
)

equipment = load_equipment()
if not equipment:
    st.warning(
        f"No OEE time series found in space `{OEE_SPACE}`. "
        "Run the `oee_timeseries` function (or wait for its schedule) first."
    )
    st.stop()

site_map = load_site_map()

with st.sidebar:
    st.header("Filters")
    sites_present = sorted({site_map.get(eq, "Unknown") for eq in equipment})
    selected_sites = st.multiselect("Sites", sites_present, default=sites_present)
    lookback_label = st.selectbox("Time range", list(LOOKBACK_OPTIONS), index=2)
    if st.button("Refresh data"):
        st.cache_data.clear()
        st.rerun()

filtered = sorted(eq for eq in equipment if site_map.get(eq, "Unknown") in selected_sites)
if not filtered:
    st.info("No equipment for the selected sites.")
    st.stop()

latest_df = load_latest(tuple(filtered))
if latest_df.empty:
    st.warning("OEE time series exist but have no datapoints yet.")
    st.stop()

latest_df.insert(0, "site", [site_map.get(eq, "Unknown") for eq in latest_df.index])
for metric in ALL_METRICS:
    if metric not in latest_df:
        latest_df[metric] = float("nan")

# --- KPI row (averages of the latest value across the selected equipment)
st.subheader("Current status")
cols = st.columns(5)
for col, metric in zip(cols, RATIO_METRICS):
    col.metric(metric.title() if metric != "oee" else "OEE", pct(latest_df[metric].mean()))
cols[4].metric("Off spec (last min, total)", f"{latest_df['off_spec'].sum():,.0f}")

# --- Per-site summary
st.subheader("OEE by site")
by_site = latest_df.groupby("site")[RATIO_METRICS].mean().sort_values("oee", ascending=False)
st.bar_chart(by_site["oee"] * 100, y_label="OEE (%)")

# --- Equipment table
st.subheader("Equipment")
table = latest_df[["site", *RATIO_METRICS, "off_spec", "updated"]].sort_values("oee")
st.dataframe(
    table,
    use_container_width=True,
    column_config={
        "oee": st.column_config.ProgressColumn("OEE", min_value=0, max_value=1, format="%.2f"),
        "availability": st.column_config.NumberColumn("Availability", format="%.2f"),
        "performance": st.column_config.NumberColumn("Performance", format="%.2f"),
        "quality": st.column_config.NumberColumn("Quality", format="%.2f"),
        "off_spec": st.column_config.NumberColumn("Off spec", format="%.0f"),
        "updated": st.column_config.DatetimeColumn("Last datapoint (UTC)"),
    },
)

# --- Detail view
st.subheader("Equipment trend")
selected = st.selectbox(
    "Equipment",
    table.index.tolist(),
    format_func=lambda eq: f"{eq} ({site_map.get(eq, 'Unknown')})",
)
lookback_minutes, granularity = LOOKBACK_OPTIONS[lookback_label]
history = load_history(selected, lookback_minutes, granularity)

if history.empty:
    st.info("No datapoints in the selected time range.")
else:
    ratio_cols = [m for m in RATIO_METRICS if m in history]
    st.markdown(f"**OEE components** ({lookback_label.lower()}, {granularity} average)")
    st.line_chart(history[ratio_cols] * 100, y_label="%")
    if "off_spec" in history:
        st.markdown("**Off spec items per minute**")
        st.bar_chart(history["off_spec"])
