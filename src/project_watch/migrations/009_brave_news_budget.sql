-- Reserve paid Brave News requests atomically before egress. Count attempts, even failures.
CREATE TABLE brave_news_budget (
    month_utc text PRIMARY KEY,
    used integer NOT NULL CHECK (used >= 0)
);
