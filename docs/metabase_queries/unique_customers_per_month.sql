-- Unique customers per month
-- Card ID: 30
-- Collection: Examples
-- Updated: 2026-05-05T19:29:01.38067Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  DATE_TRUNC('quarter', "__mb_source"."CREATED_AT") AS "CREATED_AT",
  count(distinct "__mb_source"."People - User__EMAIL") AS "count"
FROM
  (
    SELECT
      "PUBLIC"."ORDERS"."ID" AS "ID",
      "PUBLIC"."ORDERS"."USER_ID" AS "USER_ID",
      "PUBLIC"."ORDERS"."PRODUCT_ID" AS "PRODUCT_ID",
      "PUBLIC"."ORDERS"."SUBTOTAL" AS "SUBTOTAL",
      "PUBLIC"."ORDERS"."TAX" AS "TAX",
      "PUBLIC"."ORDERS"."TOTAL" AS "TOTAL",
      "PUBLIC"."ORDERS"."DISCOUNT" AS "DISCOUNT",
      "PUBLIC"."ORDERS"."CREATED_AT" AS "CREATED_AT",
      "PUBLIC"."ORDERS"."QUANTITY" AS "QUANTITY",
      (
        DATEDIFF(
          month,
          CAST("People - User"."BIRTH_DATE" AS timestamp),
          NOW()
        ) + CASE
          WHEN ("People - User"."BIRTH_DATE" < NOW())
          AND (
            extract(
              day
              from
                CAST("People - User"."BIRTH_DATE" AS timestamp)
            ) > extract(
              day
              from
                NOW()
            )
          ) THEN -1
          WHEN ("People - User"."BIRTH_DATE" > NOW())
          AND (
            extract(
              day
              from
                CAST("People - User"."BIRTH_DATE" AS timestamp)
            ) < extract(
              day
              from
                NOW()
            )
          ) THEN 1
          ELSE 0
        END
      ) / 12 AS "Age",
      "People - User"."EMAIL" AS "People - User__EMAIL",
      "People - User"."STATE" AS "People - User__STATE",
      "PEOPLE__via__USER_ID"."NAME" AS "PEOPLE__via__USER_ID__NAME",
      "PRODUCTS__via__PRODUCT_ID"."TITLE" AS "PRODUCTS__via__PRODUCT_ID__TITLE"
    FROM
      "PUBLIC"."ORDERS"
      LEFT JOIN (
        SELECT
          "PUBLIC"."PEOPLE"."ID" AS "ID",
          "PUBLIC"."PEOPLE"."ADDRESS" AS "ADDRESS",
          "PUBLIC"."PEOPLE"."EMAIL" AS "EMAIL",
          "PUBLIC"."PEOPLE"."PASSWORD" AS "PASSWORD",
          "PUBLIC"."PEOPLE"."NAME" AS "NAME",
          "PUBLIC"."PEOPLE"."CITY" AS "CITY",
          "PUBLIC"."PEOPLE"."LONGITUDE" AS "LONGITUDE",
          "PUBLIC"."PEOPLE"."STATE" AS "STATE",
          "PUBLIC"."PEOPLE"."SOURCE" AS "SOURCE",
          "PUBLIC"."PEOPLE"."BIRTH_DATE" AS "BIRTH_DATE",
          "PUBLIC"."PEOPLE"."ZIP" AS "ZIP",
          "PUBLIC"."PEOPLE"."LATITUDE" AS "LATITUDE",
          "PUBLIC"."PEOPLE"."CREATED_AT" AS "CREATED_AT"
        FROM
          "PUBLIC"."PEOPLE"
      ) AS "People - User" ON "PUBLIC"."ORDERS"."USER_ID" = "People - User"."ID"
      LEFT JOIN (
        SELECT
          "PUBLIC"."PEOPLE"."ID" AS "ID",
          "PUBLIC"."PEOPLE"."ADDRESS" AS "ADDRESS",
          "PUBLIC"."PEOPLE"."EMAIL" AS "EMAIL",
          "PUBLIC"."PEOPLE"."PASSWORD" AS "PASSWORD",
          "PUBLIC"."PEOPLE"."NAME" AS "NAME",
          "PUBLIC"."PEOPLE"."CITY" AS "CITY",
          "PUBLIC"."PEOPLE"."LONGITUDE" AS "LONGITUDE",
          "PUBLIC"."PEOPLE"."STATE" AS "STATE",
          "PUBLIC"."PEOPLE"."SOURCE" AS "SOURCE",
          "PUBLIC"."PEOPLE"."BIRTH_DATE" AS "BIRTH_DATE",
          "PUBLIC"."PEOPLE"."ZIP" AS "ZIP",
          "PUBLIC"."PEOPLE"."LATITUDE" AS "LATITUDE",
          "PUBLIC"."PEOPLE"."CREATED_AT" AS "CREATED_AT"
        FROM
          "PUBLIC"."PEOPLE"
      ) AS "PEOPLE__via__USER_ID" ON "PUBLIC"."ORDERS"."USER_ID" = "PEOPLE__via__USER_ID"."ID"
      LEFT JOIN (
        SELECT
          "PUBLIC"."PRODUCTS"."ID" AS "ID",
          "PUBLIC"."PRODUCTS"."EAN" AS "EAN",
          "PUBLIC"."PRODUCTS"."TITLE" AS "TITLE",
          "PUBLIC"."PRODUCTS"."CATEGORY" AS "CATEGORY",
          "PUBLIC"."PRODUCTS"."VENDOR" AS "VENDOR",
          "PUBLIC"."PRODUCTS"."PRICE" AS "PRICE",
          "PUBLIC"."PRODUCTS"."RATING" AS "RATING",
          "PUBLIC"."PRODUCTS"."CREATED_AT" AS "CREATED_AT"
        FROM
          "PUBLIC"."PRODUCTS"
      ) AS "PRODUCTS__via__PRODUCT_ID" ON "PUBLIC"."ORDERS"."PRODUCT_ID" = "PRODUCTS__via__PRODUCT_ID"."ID"
  ) AS "__mb_source"
WHERE
  (
    "__mb_source"."CREATED_AT" >= DATE_TRUNC(
      'quarter',
      DATEADD('month', CAST(-2 * 3 AS integer), NOW())
    )
  )
  AND (
    "__mb_source"."CREATED_AT" < DATE_TRUNC('quarter', NOW())
  )
GROUP BY
  DATE_TRUNC('quarter', "__mb_source"."CREATED_AT")
ORDER BY
  DATE_TRUNC('quarter', "__mb_source"."CREATED_AT") ASC
