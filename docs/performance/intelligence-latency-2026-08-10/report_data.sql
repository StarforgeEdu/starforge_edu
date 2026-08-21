-- Reproducible report-layer transforms over measurements transcribed from evidence.md.
-- These VALUES statements do not claim to be the production monitoring query;
-- they produce the bounded datasets embedded in artifact.json.

WITH measurements(label, before_floor_s, after_s, comparison_basis, layer, display_order) AS (
    VALUES
        ('Risk selector', 30.0, 0.2280, '30 s lower bound', 'Set-based SQL', 1),
        ('Risk live cold', 30.0, 0.1086, '30 s lower bound', 'Cold private snapshot', 2),
        ('Risk live warm', 30.0, 0.0228, '30 s lower bound', 'Warm private snapshot', 3),
        ('Executive selector', 60.0, 0.0887, '60 s timeout floor', 'Set-based SQL', 4),
        ('Executive cache hit', 60.0, 0.1273, '60 s timeout floor; slower observed hit', 'Warm private snapshot', 5)
)
SELECT
    label,
    before_floor_s,
    after_s,
    ROUND((before_floor_s / after_s)::numeric, 1) AS minimum_speedup_x,
    comparison_basis,
    layer,
    display_order
FROM measurements
ORDER BY display_order;

SELECT *
FROM (VALUES
    (1, 'p50 (median)', 0.121),
    (2, 'p90', 0.923),
    (3, 'p95', 2.400),
    (4, 'p99', 49.863),
    (5, 'Maximum', 58.141)
) AS baseline(display_order, statistic, backend_duration_s)
ORDER BY display_order;

SELECT *
FROM (VALUES
    (1, 'p50 (median)', 0.0291),
    (2, 'p95', 0.3322),
    (3, 'p99', 0.7776),
    (4, 'Maximum', 1.4249)
) AS post_deploy(display_order, statistic, backend_duration_s)
ORDER BY display_order;

SELECT *
FROM (VALUES
    (1, 'Risk selector', '30–58 s', 0.2280, 'Selector', 'Exact first-100 rows and 1,200 total matched'),
    (2, 'Risk endpoint, cold', '30–58 s', 0.1086, 'Cache miss', 'Authenticated live request'),
    (3, 'Risk endpoint, warm', '30–58 s', 0.0228, 'Cache hit', 'Authenticated live request'),
    (4, 'Executive selector', '>60 s timeout', 0.0887, 'Selector', 'Four database queries'),
    (5, 'Executive cache hit A', '>60 s timeout', 0.0540, 'Cache hit', 'Authenticated live request'),
    (6, 'Executive cache hit B', '>60 s timeout', 0.1273, 'Cache hit', 'Authenticated live request')
) AS verification(display_order, measurement, before_observation, after_s, state, verification_note)
ORDER BY display_order;

SELECT *
FROM (VALUES
    (1, 'Before', 5925831.91, 20, 'Repeated correlated attendance, grade, and invoice subqueries'),
    (2, 'After', 6209.00, 0, 'Three grouped signals plus bounded student-page hydration')
) AS plan_comparison(display_order, implementation, planner_cost, correlated_subplans, query_shape)
ORDER BY display_order;
