import requests

query = """
query getProduct($handle: String!) {
  product(handle: $handle) {
    id
    title
    variants(first: 10) {
      edges {
        node {
          id
          title
          availableForSale
        }
      }
    }
  }
}
"""

res = requests.post(
    "https://www.ornotbike.com/api/2024-01/graphql.json",
    json={"query": query, "variables": {"handle": "cargo-bib-short-black"}},
    headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
)
print("Storefront GraphQL Status:", res.status_code)
print("Storefront GraphQL Response:", res.text[:400])
