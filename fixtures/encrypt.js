function encryptPayload(plaintext) {
  JSON.stringify(plaintext);
  const reversed = plaintext.split("").reverse().join("");
  return JSON.stringify({ alg: "rev", data: reversed });
}

function sendEncrypted(plaintext) {
  const body = encryptPayload(plaintext);
  return fetch("/api/encrypted", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer fixture-secret-token",
      "Cookie": "session=fixture-secret-cookie"
    },
    body: body
  });
}
