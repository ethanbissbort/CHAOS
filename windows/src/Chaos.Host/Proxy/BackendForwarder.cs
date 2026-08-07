using System.Diagnostics;
using System.Net;
using System.Text.Json;
using Chaos.Host.Configuration;
using Chaos.Host.Health;
using Chaos.Host.Routing;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Yarp.ReverseProxy.Forwarder;

namespace Chaos.Host.Proxy;

/// <summary>
/// Forwards a Python-owned request to the platform backend using YARP's direct
/// forwarding API, and turns a forwarding failure into an answer an operator can act on.
/// </summary>
/// <remarks>
/// <para>
/// <b>Why direct forwarding rather than YARP's route/cluster configuration.</b>
/// Ownership is decided by <see cref="RouteOwnershipTable"/> and nothing else.
/// Expressing the same decision a second time as YARP routes would put ASP.NET
/// Core route-precedence rules in charge of which component serves a path, and
/// the entire point of the manifest is that exactly one artefact answers that
/// question. <see cref="IHttpForwarder"/> gives the same streaming proxy engine
/// — header handling, request/response streaming, no buffering — driven by the
/// manifest.
/// </para>
/// <para>
/// Responses stream: YARP copies the response body as it arrives, and
/// automatic decompression is disabled so bytes pass through untouched.
/// </para>
/// </remarks>
internal sealed partial class BackendForwarder : IDisposable
{
    private readonly IHttpForwarder _forwarder;
    private readonly HttpMessageInvoker _invoker;
    private readonly ForwarderRequestConfig _requestConfig;
    private readonly BackendRequestTransformer _transformer;
    private readonly BackendHealthState _health;
    private readonly ChaosHostOptions _options;
    private readonly ILogger<BackendForwarder> _logger;
    private readonly string _destinationPrefix;

    public BackendForwarder(
        IHttpForwarder forwarder,
        BackendHealthState health,
        IOptions<ChaosHostOptions> options,
        ILogger<BackendForwarder> logger)
    {
        _forwarder = forwarder;
        _health = health;
        _options = options.Value;
        _logger = logger;
        _destinationPrefix = _options.BackendUri().ToString();
        _transformer = new BackendRequestTransformer(_options);

        _requestConfig = new ForwarderRequestConfig
        {
            ActivityTimeout = _options.RequestTimeout,
            VersionPolicy = HttpVersionPolicy.RequestVersionOrLower,
            Version = HttpVersion.Version11,
        };

        _invoker = new HttpMessageInvoker(new SocketsHttpHandler
        {
            UseProxy = false,
            AllowAutoRedirect = false,
            AutomaticDecompression = DecompressionMethods.None,
            UseCookies = false,
            ConnectTimeout = TimeSpan.FromSeconds(5),
            ActivityHeadersPropagator = new ReverseProxyPropagator(DistributedContextPropagator.Current),
        });
    }

    /// <summary>
    /// Forwards the current request to the backend, writing a gateway error
    /// response if forwarding fails.
    /// </summary>
    /// <param name="context">The request being proxied.</param>
    /// <param name="entry">The manifest row that claimed this path.</param>
    /// <returns>A task that completes when the response has been written.</returns>
    public async Task ForwardAsync(HttpContext context, RouteOwnershipEntry entry)
    {
        var error = await _forwarder
            .SendAsync(context, _destinationPrefix, _invoker, _requestConfig, _transformer)
            .ConfigureAwait(false);

        if (error == ForwarderError.None)
        {
            return;
        }

        await WriteGatewayErrorAsync(context, entry, error).ConfigureAwait(false);
    }

    private async Task WriteGatewayErrorAsync(HttpContext context, RouteOwnershipEntry entry, ForwarderError error)
    {
        var feature = context.Features.Get<IForwarderErrorFeature>();
        var exception = feature?.Exception;
        var cause = Describe(exception);

        if (context.RequestAborted.IsCancellationRequested || IsClientAbort(error))
        {
            LogClientAborted(context.Request.Path, error);
            return;
        }

        if (context.Response.HasStarted)
        {
            // The backend began answering and then failed mid-stream. The status
            // line is already on the wire; all we can honestly do is record it.
            LogFailedAfterResponseStarted(context.Request.Path, error, cause);
            return;
        }

        var (status, code, detail) = Classify(error);
        var snapshot = _health.Current;

        LogForwardFailed(context.Request.Path, error, status, cause);

        context.Response.Clear();
        context.Response.StatusCode = status;
        context.Response.ContentType = "application/json; charset=utf-8";
        context.Response.Headers.CacheControl = "no-store";
        if (status is StatusCodes.Status503ServiceUnavailable)
        {
            context.Response.Headers.RetryAfter = "5";
        }

        var body = new
        {
            error = code,
            detail,
            path = context.Request.Path.Value,
            route = new
            {
                prefix = entry.PathPrefix,
                owner = entry.Owner.ToString(),
                source = entry.Source.ToString(),
            },
            backend = new
            {
                url = _options.BackendUrl,
                status = snapshot.BackendWireValue,
                lastError = snapshot.LastError,
                lastCheckedUtc = snapshot.LastCheckedUtc,
                lastSuccessUtc = snapshot.LastSuccessUtc,
                consecutiveFailures = snapshot.ConsecutiveFailures,
            },
            gateway = new
            {
                component = "Chaos.Host",
                version = HostVersion.Version,
                forwarderError = error.ToString(),
                cause,
            },
            hint = "This is the .NET gateway reporting that it could not complete a request against the Python "
                 + "platform backend. GET /health on this host reports backend state; GET /host/routes shows which "
                 + "component owns this path.",
        };

        await context.Response
            .WriteAsync(JsonSerializer.Serialize(body, JsonOptions), context.RequestAborted)
            .ConfigureAwait(false);
    }

    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);

    private static (int Status, string Code, string Detail) Classify(ForwarderError error) => error switch
    {
        ForwarderError.RequestTimedOut => (
            StatusCodes.Status504GatewayTimeout,
            "backend_timeout",
            "The Python platform backend did not answer within the gateway's request timeout."),

        ForwarderError.Request or ForwarderError.RequestCreation => (
            StatusCodes.Status503ServiceUnavailable,
            "backend_unavailable",
            "The .NET gateway could not reach the Python platform backend. The gateway is running; the platform "
          + "behind it is not answering."),

        ForwarderError.RequestBodyDestination
            or ForwarderError.ResponseBodyDestination
            or ForwarderError.ResponseHeaders => (
            StatusCodes.Status502BadGateway,
            "backend_protocol_error",
            "The Python platform backend answered, but the exchange failed before it completed."),

        _ => (
            StatusCodes.Status502BadGateway,
            "backend_error",
            "The request to the Python platform backend failed."),
    };

    private static bool IsClientAbort(ForwarderError error) => error
        is ForwarderError.RequestCanceled
        or ForwarderError.RequestBodyCanceled
        or ForwarderError.ResponseBodyCanceled
        or ForwarderError.UpgradeRequestCanceled
        or ForwarderError.UpgradeResponseCanceled;

    private static string? Describe(Exception? exception)
    {
        if (exception is null)
        {
            return null;
        }

        var inner = exception.InnerException;
        return inner is null
            ? $"{exception.GetType().Name}: {exception.Message}"
            : $"{exception.GetType().Name}: {exception.Message} ({inner.GetType().Name}: {inner.Message})";
    }

    public void Dispose() => _invoker.Dispose();

    [LoggerMessage(EventId = 2001, Level = LogLevel.Warning,
        Message = "Proxying {Path} to the platform backend failed ({ForwarderError}); answered {StatusCode}. {Cause}")]
    private partial void LogForwardFailed(PathString path, ForwarderError forwarderError, int statusCode, string? cause);

    [LoggerMessage(EventId = 2002, Level = LogLevel.Error,
        Message = "Proxying {Path} failed after the response had started ({ForwarderError}); the client received a truncated response. {Cause}")]
    private partial void LogFailedAfterResponseStarted(PathString path, ForwarderError forwarderError, string? cause);

    [LoggerMessage(EventId = 2003, Level = LogLevel.Debug,
        Message = "Client aborted {Path} while it was being proxied ({ForwarderError}).")]
    private partial void LogClientAborted(PathString path, ForwarderError forwarderError);
}
