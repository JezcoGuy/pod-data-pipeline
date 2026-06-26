"""
shopify_api.py
==============
Thin Shopify GraphQL helpers for best_seller_sync (v2).

Mirrors the auth + mutation patterns from shopify_tag_sync_hero.py:
add/remove tags via productUpdate. No REST/collection helpers — v2 does
not touch collections directly.

Loads .env. SHOPIFY_STORE_NAME=c71080-84 is suffixed with
`.myshopify.com` automatically (same as the rest of the pipeline scripts).
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

REQUEST_TIMEOUT = 30


def get_env():
    shop_raw = os.getenv("SHOP_DOMAIN") or os.getenv("SHOPIFY_STORE_NAME")
    token = os.getenv("SHOPIFY_ACCESS_TOKEN")
    version = os.getenv("API_VERSION", "2025-04")

    if not shop_raw or not token:
        raise RuntimeError("Missing SHOPIFY_STORE_NAME/SHOP_DOMAIN or SHOPIFY_ACCESS_TOKEN in .env")

    shop = shop_raw if "." in shop_raw else f"{shop_raw}.myshopify.com"
    endpoint = f"https://{shop}/admin/api/{version}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }
    return endpoint, headers


def to_gid(pid):
    return f"gid://shopify/Product/{pid}"


def graphql(endpoint, headers, query, variables=None):
    r = requests.post(
        endpoint,
        headers=headers,
        json={"query": query, "variables": variables},
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"GraphQL HTTP {r.status_code}: {r.text}")
    payload = r.json()
    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload


def get_products_with_tag(endpoint, headers, tag):
    """Returns [{id, tags}] for every product currently carrying `tag`."""
    query = """
    query ($query: String!, $cursor: String) {
      products(first: 250, query: $query, after: $cursor) {
        edges {
          cursor
          node { id tags }
        }
        pageInfo { hasNextPage }
      }
    }
    """
    cursor = None
    products = []
    while True:
        variables = {"query": f"tag:{tag}", "cursor": cursor}
        data = graphql(endpoint, headers, query, variables)
        edges = data["data"]["products"]["edges"]
        for edge in edges:
            products.append(edge["node"])
        if not data["data"]["products"]["pageInfo"]["hasNextPage"]:
            break
        cursor = edges[-1]["cursor"]
    return products


def _product_update_tags(endpoint, headers, gid, tags):
    mutation = """
    mutation ($id: ID!, $tags: [String!]) {
      productUpdate(input: {id: $id, tags: $tags}) {
        product { id }
        userErrors { field message }
      }
    }
    """
    result = graphql(endpoint, headers, mutation, {"id": gid, "tags": tags})
    errors = result["data"]["productUpdate"]["userErrors"]
    if errors:
        raise RuntimeError(f"productUpdate userErrors for {gid}: {errors}")


def add_tag_to_product(endpoint, headers, gid, tag):
    """Fetches current tags, adds `tag`, writes back. Returns True if applied, False if already present."""
    query = "query ($id: ID!) { product(id: $id) { id tags } }"
    product = graphql(endpoint, headers, query, {"id": gid})["data"]["product"]
    if product is None:
        raise RuntimeError(f"Product not found: {gid}")
    tags = set(product["tags"])
    if tag in tags:
        return False
    tags.add(tag)
    _product_update_tags(endpoint, headers, gid, sorted(tags))
    return True


def remove_tag_from_product(endpoint, headers, gid, tag, cached_tags=None):
    """Removes `tag` from product. Uses cached_tags if supplied (skips lookup)."""
    if cached_tags is None:
        query = "query ($id: ID!) { product(id: $id) { id tags } }"
        product = graphql(endpoint, headers, query, {"id": gid})["data"]["product"]
        if product is None:
            raise RuntimeError(f"Product not found: {gid}")
        tags = set(product["tags"])
    else:
        tags = set(cached_tags)

    if tag not in tags:
        return False
    tags.remove(tag)
    _product_update_tags(endpoint, headers, gid, sorted(tags))
    return True
