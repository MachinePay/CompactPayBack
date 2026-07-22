# AWS IoT Core certificates

Place the backend device certificates here when running locally:

- `AmazonRootCA1.pem`
- `backend-certificate.pem.crt`
- `backend-private.pem.key`

Do not commit certificate or private key files. In Render, prefer Secret Files or environment variables pointing `AWS_CA_PATH`, `AWS_CERT_PATH`, and `AWS_KEY_PATH` to the secure file locations.
