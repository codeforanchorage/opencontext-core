"""AWS Lambda adapter for OpenContext MCP server.

This adapter transforms AWS Lambda events (from Function URL or API Gateway)
into the universal HTTP format expected by UniversalHTTPHandler.
"""

import asyncio
import base64
import json
import logging
from typing import Any, Dict, Optional, Protocol

from server.http_handler import (
    UNDECODABLE_BODY,
    UniversalHTTPHandler,
    normalize_headers,
)


class LambdaContext(Protocol):
    """Protocol for AWS Lambda context object.

    This defines the expected interface for Lambda context objects,
    which provide runtime information about the Lambda execution environment.
    """

    aws_request_id: str
    function_name: Optional[str]
    memory_limit_in_mb: Optional[int]


logger = logging.getLogger(__name__)

# Module-level handler instance for Lambda warm starts
_handler: Optional[UniversalHTTPHandler] = None


def get_handler() -> UniversalHTTPHandler:
    """Get or create the universal HTTP handler instance.

    Uses lazy initialization to support Lambda warm starts.

    Returns:
        UniversalHTTPHandler instance
    """
    global _handler

    if _handler is None:
        _handler = UniversalHTTPHandler()
        logger.info("Created new UniversalHTTPHandler instance")

    return _handler


def lambda_handler(
    event: Dict[str, Any], context: Optional[LambdaContext]
) -> Dict[str, Any]:
    """AWS Lambda handler function.

    Transforms Lambda events to universal HTTP format, processes the request,
    and transforms the response back to Lambda format.

    Supports both Lambda Function URL and API Gateway event formats.

    Args:
        event: Lambda event (HTTP request from Function URL or API Gateway)
        context: Lambda context object

    Returns:
        HTTP response dictionary with statusCode, headers, and body
    """
    try:
        # Extract request ID from context
        request_id = context.aws_request_id if context else "unknown"
        function_name = getattr(context, "function_name", None) if context else None
        memory_limit = getattr(context, "memory_limit_in_mb", None) if context else None

        logger.info(
            "Lambda invocation started",
            extra={
                "request_id": request_id,
                "function_name": function_name,
                "memory_limit": memory_limit,
            },
        )

        # Extract HTTP method (supports both formats)
        request_context = event.get("requestContext") or {}
        http_method = (request_context.get("http") or {}).get(
            "method"
        ) or event.get("httpMethod", "POST")

        # Extract path (supports both formats)
        request_path = (
            event.get("rawPath")
            or (request_context.get("http") or {}).get("path")
            or event.get("path", "/")
        )

        # Headers, normalised by the handler's own function so the
        # preflight, the request and the error path all read a request's
        # Origin the same way (HTTP header names are case-insensitive, and
        # Function URL and API Gateway events present them differently).
        request_headers = normalize_headers(event.get("headers"))

        # Handle OPTIONS requests for CORS preflight
        if http_method == "OPTIONS":
            handler = get_handler()
            status_code, headers, body = handler.handle_options(
                request_id=request_id,
                request_origin=request_headers.get("origin"),
            )

            logger.info(
                "CORS preflight request handled",
                extra={
                    "request_id": request_id,
                    "status_code": status_code,
                },
            )

            return {
                "statusCode": status_code,
                "headers": headers,
                "body": body,
            }

        # Extract body
        # API Gateway sends "body": null (key present) for bodiless requests,
        # so .get()'s default never applies; json.loads(None) is a TypeError
        # the handler doesn't catch -> 500. "" parses to a clean -32700.
        body = event.get("body") or ""

        # Handle base64-encoded bodies from API Gateway
        if event.get("isBase64Encoded", False):
            # Hand the handler the decoded BYTES: it measures the size
            # limit on them and decodes them strictly, refusing anything
            # that is not UTF-8. A body that is not valid base64 at all is
            # the caller's mistake too, so it becomes the same
            # unparseable marker (400 / -32700), not a 500 from here.
            # Whitespace is removed first: a hand-built or MIME-wrapped
            # event may carry newlines, which strict decoding rejects and
            # which were tolerated before decoding became strict.
            try:
                if isinstance(body, str):
                    body = "".join(body.split())
                body = base64.b64decode(body, validate=True)
            except Exception as e:
                logger.warning(
                    f"Undecodable base64 body: {type(e).__name__}",
                    extra={"request_id": request_id},
                )
                body = UNDECODABLE_BODY

        # A direct invoke can carry an already-parsed body. The handler
        # accepts that too; converting here keeps what it receives a
        # string, which is what this adapter has always passed.
        if isinstance(body, (dict, list)):
            body = json.dumps(body)

        headers = request_headers

        # Get handler and process request
        handler = get_handler()

        async def _run_with_cleanup():
            try:
                return await handler.handle_request(
                    method=http_method,
                    path=request_path,
                    body=body,
                    headers=headers,
                    request_id=request_id,
                )
            finally:
                # Close plugin HTTP clients before event loop closes.
                # Fixes "Event loop is closed" on warm starts when tools use httpx.
                from server import http_handler

                if http_handler._plugin_manager is not None:
                    await http_handler._plugin_manager.shutdown()
                    http_handler._plugin_manager = None
                    http_handler._mcp_server = None

        # Run async handler
        status_code, response_headers, response_body = asyncio.run(_run_with_cleanup())

        # Transform to Lambda response format
        lambda_response = {
            "statusCode": status_code,
            "headers": response_headers,
            "body": response_body,
        }

        logger.info(
            "Lambda invocation completed",
            extra={
                "request_id": request_id,
                "status_code": status_code,
            },
        )

        return lambda_response

    except Exception as e:
        # Comprehensive error handling - catch all exceptions and return 500
        request_id = context.aws_request_id if context else "unknown"

        logger.error(
            f"Error in Lambda handler: {e}",
            extra={
                "request_id": request_id,
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )

        # Reflect Origin against the allowlist so even error responses
        # respect CORS scope. Browsers ignore mismatches; native clients
        # ignore CORS entirely.
        # This is the error path: assume nothing about the event's shape.
        error_origin = normalize_headers(
            event.get("headers") if isinstance(event, dict) else None
        ).get("origin")
        # Don't leak exception details to the client; full traceback is in
        # CloudWatch via logger.error(..., exc_info=True) above.
        error_cors = UniversalHTTPHandler._get_cors_headers(error_origin)
        error_response = {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json",
                **error_cors,
            },
            "body": json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": "Internal error",
                        "data": f"Request ID: {request_id}",
                    },
                }
            ),
        }

        return error_response
