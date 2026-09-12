-- =============================================================================
-- Chicago CTA Transit Analytics — SQL Query Library
-- =============================================================================
-- All queries run against the processed tables populated by processing.py.
-- Database: data/cta_data.db (SQLite)
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Q1: Average delay by route (overall, peak, and off-peak)
-- -----------------------------------------------------------------------------
-- Identifies which routes are chronically delayed and whether peak hours
-- amplify the problem disproportionately.
-- -----------------------------------------------------------------------------
SELECT
    rtnm                                                              AS route_name,
    rt                                                                AS route_id,
    COUNT(*)                                                          AS total_observations,
    ROUND(AVG(delay_minutes), 2)                                      AS avg_delay_min,
    ROUND(AVG(CASE WHEN is_peak    = 1 THEN delay_minutes END), 2)   AS peak_delay_min,
    ROUND(AVG(CASE WHEN is_peak    = 0 THEN delay_minutes END), 2)   AS offpeak_delay_min,
    ROUND(AVG(CASE WHEN is_weekend = 1 THEN delay_minutes END), 2)   AS weekend_delay_min,
    ROUND(MAX(delay_minutes), 2)                                      AS max_delay_min
FROM vehicles_processed
GROUP BY rt, rtnm
ORDER BY avg_delay_min DESC;


-- -----------------------------------------------------------------------------
-- Q2: Busiest routes by hour of day
-- -----------------------------------------------------------------------------
-- Shows how vehicle density changes throughout the day per route,
-- useful for scheduling and capacity planning.
-- -----------------------------------------------------------------------------
SELECT
    rtnm                        AS route_name,
    hour_of_day,
    COUNT(*)                    AS vehicle_count,
    ROUND(AVG(delay_minutes), 2) AS avg_delay_min,
    SUM(is_delayed)             AS delayed_vehicles
FROM vehicles_processed
GROUP BY rtnm, hour_of_day
ORDER BY vehicle_count DESC
LIMIT 100;


-- -----------------------------------------------------------------------------
-- Q3: On-time performance by route and time window (peak vs off-peak)
-- -----------------------------------------------------------------------------
-- Core KPI for transit reliability reporting.
-- On-time = delay_minutes <= 3.0 (industry standard threshold).
-- -----------------------------------------------------------------------------
SELECT
    rtnm                                                                    AS route_name,
    CASE WHEN is_peak = 1 THEN 'Peak' ELSE 'Off-Peak' END                  AS time_window,
    COUNT(*)                                                                AS total_vehicles,
    SUM(CASE WHEN is_delayed = 0 THEN 1 ELSE 0 END)                        AS on_time_vehicles,
    ROUND(
        SUM(CASE WHEN is_delayed = 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*),
        1
    )                                                                       AS pct_on_time,
    ROUND(AVG(delay_minutes), 2)                                            AS avg_delay_min
FROM vehicles_processed
GROUP BY rtnm, is_peak
ORDER BY rtnm, is_peak DESC;


-- -----------------------------------------------------------------------------
-- Q4: Service alert frequency and severity by route
-- -----------------------------------------------------------------------------
-- Highlights which routes generate the most operational disruptions,
-- correlating alert volume with route delay performance.
-- -----------------------------------------------------------------------------
SELECT
    a.rt                                                          AS route_id,
    COALESCE(r.rtnm, a.rt)                                        AS route_name,
    COUNT(*)                                                      AS alert_count,
    ROUND(AVG(a.severity_score), 2)                               AS avg_severity,
    SUM(CASE WHEN a.severity_score = 3 THEN 1 ELSE 0 END)         AS high_severity_alerts,
    GROUP_CONCAT(DISTINCT a.impact)                               AS alert_types
FROM raw_alerts a
LEFT JOIN (
    SELECT rt, rtnm FROM raw_routes GROUP BY rt HAVING MAX(id)
) r ON a.rt = r.rt
GROUP BY a.rt
ORDER BY alert_count DESC;


-- -----------------------------------------------------------------------------
-- Q5: Peak vs off-peak headway comparison by route
-- -----------------------------------------------------------------------------
-- Headway (gap between buses) is a key service frequency metric.
-- Longer peak headway = crowded buses and poor passenger experience.
-- -----------------------------------------------------------------------------
SELECT
    rtnm                                                                            AS route_name,
    ROUND(AVG(CASE WHEN is_peak = 1    THEN headway_minutes END), 2)                AS peak_headway_min,
    ROUND(AVG(CASE WHEN is_peak = 0
                    AND is_weekend = 0  THEN headway_minutes END), 2)               AS offpeak_headway_min,
    ROUND(AVG(CASE WHEN is_weekend = 1  THEN headway_minutes END), 2)               AS weekend_headway_min,
    ROUND(AVG(headway_minutes), 2)                                                  AS overall_headway_min,
    -- Ratio > 1 means worse (longer wait) during peak
    ROUND(
        AVG(CASE WHEN is_peak = 1 THEN headway_minutes END) /
        NULLIF(AVG(CASE WHEN is_peak = 0 AND is_weekend = 0 THEN headway_minutes END), 0),
        2
    )                                                                               AS peak_vs_offpeak_ratio
FROM vehicles_processed
WHERE headway_minutes IS NOT NULL
  AND headway_minutes > 0
  AND headway_minutes < 90   -- exclude outliers (first vehicle of the day)
GROUP BY rtnm
ORDER BY peak_headway_min DESC;


-- -----------------------------------------------------------------------------
-- Q6: Top 10 most delayed stops (by percentage of delayed predictions)
-- -----------------------------------------------------------------------------
-- Stop-level analysis reveals geographic and operational choke points.
-- Minimum 10 observations required for statistical significance.
-- -----------------------------------------------------------------------------
SELECT
    stpnm                                                           AS stop_name,
    rt                                                              AS route_id,
    COUNT(*)                                                        AS prediction_count,
    SUM(is_delayed)                                                 AS delayed_count,
    ROUND(SUM(is_delayed) * 100.0 / COUNT(*), 1)                    AS pct_delayed,
    ROUND(AVG(minutes_to_arr), 2)                                   AS avg_wait_min,
    ROUND(AVG(CASE WHEN is_delayed = 1 THEN minutes_to_arr END), 2) AS avg_delayed_wait_min
FROM predictions_processed
GROUP BY stpnm, rt
HAVING COUNT(*) >= 10
ORDER BY pct_delayed DESC, avg_wait_min DESC
LIMIT 10;


-- -----------------------------------------------------------------------------
-- Q7: Day-of-week delay and ridership patterns
-- -----------------------------------------------------------------------------
-- Quantifies how Monday through Sunday differ in delay and vehicle volume,
-- supporting decisions about supplemental service on high-demand days.
-- -----------------------------------------------------------------------------
SELECT
    CASE day_of_week
        WHEN 0 THEN 'Monday'    WHEN 1 THEN 'Tuesday'
        WHEN 2 THEN 'Wednesday' WHEN 3 THEN 'Thursday'
        WHEN 4 THEN 'Friday'    WHEN 5 THEN 'Saturday'
        WHEN 6 THEN 'Sunday'
    END                                                                 AS day_name,
    day_of_week,
    COUNT(*)                                                            AS total_vehicles,
    ROUND(AVG(delay_minutes), 2)                                        AS avg_delay_min,
    ROUND(
        SUM(CASE WHEN is_delayed = 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*),
        1
    )                                                                   AS pct_on_time,
    ROUND(AVG(headway_minutes), 2)                                      AS avg_headway_min
FROM vehicles_processed
WHERE headway_minutes IS NOT NULL
GROUP BY day_of_week
ORDER BY day_of_week;


-- -----------------------------------------------------------------------------
-- Q8: On-time performance trend over time (daily rollup)
-- -----------------------------------------------------------------------------
-- Time-series view for tracking whether service quality is improving or
-- degrading — useful for executive dashboards and stakeholder reports.
-- -----------------------------------------------------------------------------
SELECT
    DATE(recorded_at)                                                       AS stat_date,
    COUNT(*)                                                                AS total_vehicles,
    SUM(CASE WHEN is_delayed = 0 THEN 1 ELSE 0 END)                         AS on_time_count,
    ROUND(
        SUM(CASE WHEN is_delayed = 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*),
        1
    )                                                                       AS pct_on_time,
    ROUND(AVG(delay_minutes), 2)                                            AS avg_delay_min,
    ROUND(AVG(headway_minutes), 2)                                          AS avg_headway_min
FROM vehicles_processed
GROUP BY DATE(recorded_at)
ORDER BY stat_date;


-- -----------------------------------------------------------------------------
-- Q9: Hour-by-hour delay profile across all routes (heatmap source)
-- -----------------------------------------------------------------------------
-- Raw data for the activity heatmap: vehicle count and avg delay
-- for every (hour, day-of-week) combination.
-- -----------------------------------------------------------------------------
SELECT
    hour_of_day,
    day_of_week,
    COUNT(*)                        AS vehicle_count,
    ROUND(AVG(delay_minutes), 2)    AS avg_delay_min,
    ROUND(
        SUM(CASE WHEN is_delayed = 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*),
        1
    )                               AS pct_on_time
FROM vehicles_processed
GROUP BY hour_of_day, day_of_week
ORDER BY day_of_week, hour_of_day;


-- -----------------------------------------------------------------------------
-- Q10: Route-level weekly summary (executive overview table)
-- -----------------------------------------------------------------------------
-- One-row-per-route summary suitable for a management report or
-- the dashboard's drill-down table.
-- -----------------------------------------------------------------------------
SELECT
    rtnm                                                                    AS route_name,
    rt                                                                      AS route_id,
    COUNT(*)                                                                AS total_observations,
    ROUND(AVG(delay_minutes), 2)                                            AS avg_delay_min,
    ROUND(MAX(delay_minutes), 2)                                            AS worst_delay_min,
    ROUND(
        SUM(CASE WHEN is_delayed = 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*),
        1
    )                                                                       AS overall_on_time_pct,
    ROUND(
        SUM(CASE WHEN is_delayed = 0 AND is_peak    = 1 THEN 1 ELSE 0 END) * 100.0 /
        NULLIF(SUM(CASE WHEN is_peak = 1 THEN 1 ELSE 0 END), 0),
        1
    )                                                                       AS peak_on_time_pct,
    ROUND(
        SUM(CASE WHEN is_delayed = 0 AND is_weekend = 1 THEN 1 ELSE 0 END) * 100.0 /
        NULLIF(SUM(CASE WHEN is_weekend = 1 THEN 1 ELSE 0 END), 0),
        1
    )                                                                       AS weekend_on_time_pct,
    ROUND(AVG(headway_minutes), 2)                                          AS avg_headway_min
FROM vehicles_processed
GROUP BY rt, rtnm
ORDER BY avg_delay_min DESC;
