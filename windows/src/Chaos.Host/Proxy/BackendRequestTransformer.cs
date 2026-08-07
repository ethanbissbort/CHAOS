using System.Net;
using Chaos.Host.Configuration;
using Microsoft.AspNetCore.Http;
using Yarp.ReverseProxy.Forwarder;

namespace Chaos.Host.Proxy;

/// <summary>
/// Shapes the outbound request to the Python backend.
/// </summary>
/// <remarks>
/// <para>
/// The base <see cref="HttpTransformer"/> copies every request header except the
/// hop-by-hop ones and the original <c>Host</c>. That is exactly what this
/// gateway needs, so this class only <em>adds</em> forwarding headers and never
/// removes or rewrites anything else.
/// </para>
/// <para>
/// <b>Identity is not touched.</b> <c>X-Operator</c> and <c>X-Operator-Role</c>
/// pass through byte for byte. The gateway does not authenticate, does not
/// authorise, and above all does not invent an operator: every audited action in
/// this platform records a named actor, and a proxy that synthesised one would be
/// forging the audit trail. Authentication terminating upstream is the
/// architecture (SDD 15.3, docs/api.md section 1) — the gateway's job is to
/// carry the caller's claim to the backend unmodified, not to make one up.
/// </para>
/// </remarks>
internal sealed class BackendRequestTransformer : HttpTransformer
{
    private const string ForwardedFor = "X-Forwarded-For";
    private const string ForwardedProto = "X-Forwarded-Proto";
    private const string ForwardedHost = "X-Forwarded-Host";

    private readonly bool _trustInbound;

    public BackendRequestTransformer(ChaosHostOptions options)
    {
        _trustInbound = options.TrustInboundForwardedHeaders;
    }

    public override async ValueTask TransformRequestAsync(
        HttpContext httpContext,
        HttpRequestMessage proxyRequest,
        string destinationPrefix,
        CancellationToken cancellationToken)
    {
        await base.TransformRequestAsync(httpContext, proxyRequest, destinationPrefix, cancellationToken)
                  .ConfigureAwait(false);

        var request = httpContext.Request;
        var remoteIp = Format(httpContext.Connection.RemoteIpAddress);

        SetForwardedFor(proxyRequest, request.Headers[ForwardedFor], remoteIp);
        Replace(proxyRequest, ForwardedProto, ChooseProto(request.Headers[ForwardedProto], request.Scheme));
        Replace(proxyRequest, ForwardedHost, ChooseHost(request.Headers[ForwardedHost], request.Host.Value));
    }

    private void SetForwardedFor(HttpRequestMessage proxyRequest, string? inbound, string? remoteIp)
    {
        string? value;

        if (_trustInbound && !string.IsNullOrWhiteSpace(inbound))
        {
            // A proxy sits in front of us and we have been told to believe it.
            value = remoteIp is null ? inbound : $"{inbound}, {remoteIp}";
        }
        else
        {
            // Default: this gateway is the LAN edge. An inbound X-Forwarded-For
            // is client-supplied and unverifiable, so it is replaced with the
            // peer address we can actually observe rather than appended to.
            value = remoteIp;
        }

        Replace(proxyRequest, ForwardedFor, value);
    }

    private string ChooseProto(string? inbound, string scheme) =>
        _trustInbound && !string.IsNullOrWhiteSpace(inbound) ? inbound : scheme;

    private string? ChooseHost(string? inbound, string? host) =>
        _trustInbound && !string.IsNullOrWhiteSpace(inbound) ? inbound : host;

    private static void Replace(HttpRequestMessage proxyRequest, string name, string? value)
    {
        proxyRequest.Headers.Remove(name);
        if (!string.IsNullOrWhiteSpace(value))
        {
            proxyRequest.Headers.TryAddWithoutValidation(name, value);
        }
    }

    private static string? Format(IPAddress? address)
    {
        if (address is null)
        {
            return null;
        }

        // IPv4-mapped IPv6 (::ffff:127.0.0.1) is what Kestrel reports for an
        // IPv4 peer on a dual-stack socket; the backend logs are far more
        // readable with the plain IPv4 form.
        if (address.IsIPv4MappedToIPv6)
        {
            address = address.MapToIPv4();
        }

        return address.ToString();
    }
}
