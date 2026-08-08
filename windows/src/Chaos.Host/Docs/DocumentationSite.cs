using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.StaticFiles;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.FileProviders;

namespace Chaos.Host.Docs;

/// <summary>
/// The documentation site: a second, deliberately tiny web application that
/// serves the generated manuals and nothing else.
/// </summary>
/// <remarks>
/// <para>
/// <b>Why this is a separate application and not a path on the gateway.</b>
/// A second Kestrel endpoint on the gateway would have been fewer lines, but the
/// isolation would then be a rule enforced by a port check in middleware —
/// something a future edit can get wrong. This way the isolation is structural:
/// this application is built from its own <see cref="WebApplicationBuilder"/>,
/// with its own service collection and its own pipeline. There is no
/// <c>BackendForwarder</c> in it, no route-ownership table, no setup coordinator
/// and no proxy. A request arriving here cannot reach the platform API by any
/// path, because none of the machinery that could reach it has been built.
/// </para>
/// <para>
/// <b>Read-only, structurally.</b> The pipeline answers <c>GET</c> and
/// <c>HEAD</c> and refuses every other method with 405 before any handler runs.
/// The only file provider is a read-only view of one directory. Nothing here
/// writes anything, anywhere, and nothing here can change the state of the
/// homestead — which is what makes it safe to hand out the documentation URL to
/// anyone who is on the LAN.
/// </para>
/// <para>
/// <b>Why LAN-reachable.</b> Same posture as the gateway's own listener: bound
/// to every interface by default, firewalled to the private profile by the
/// installer. The manual is most needed on a phone, in a shed, during the
/// outage it describes; a loopback-only manual would be readable only from the
/// machine that is already the problem.
/// </para>
/// </remarks>
public static class DocumentationSite
{
    /// <summary>Methods the documentation listener answers. Everything else is 405.</summary>
    private static readonly string[] ReadOnlyMethods = ["GET", "HEAD"];

    private static readonly string[] NoCacheExtensions = [".html", ".htm", ".js", ".mjs", ".css", ".json", ".map"];

    /// <summary>
    /// Builds the documentation application. The caller starts it.
    /// </summary>
    /// <param name="root">Where the generated site was resolved to, or was not.</param>
    /// <param name="listenUrl">The address to bind, for example <c>http://0.0.0.0:8090</c>.</param>
    /// <param name="contentRootPath">
    /// Content root for the child application. Passed explicitly because the
    /// gateway may be running as a Windows Service, whose working directory is
    /// <c>C:\Windows\System32</c>; a child host that defaulted to the current
    /// directory would look for its configuration there.
    /// </param>
    /// <param name="configure">Optional hook applied to the builder before it is built. Used by tests.</param>
    /// <returns>The built application, not yet started.</returns>
    public static WebApplication Create(
        DocumentationRootResolution root,
        string listenUrl,
        string contentRootPath,
        Action<WebApplicationBuilder>? configure = null)
    {
        ArgumentNullException.ThrowIfNull(root);
        ArgumentException.ThrowIfNullOrWhiteSpace(listenUrl);
        ArgumentException.ThrowIfNullOrWhiteSpace(contentRootPath);

        // Slim: no IIS integration, no hosting startup assemblies, no
        // developer-exception middleware. A documentation server needs none of
        // it, and every component not present is one that cannot misbehave.
        var builder = WebApplication.CreateSlimBuilder(new WebApplicationOptions
        {
            ContentRootPath = contentRootPath,
            ApplicationName = typeof(DocumentationSite).Assembly.GetName().Name,
        });

        builder.WebHost.UseUrls(listenUrl);
        configure?.Invoke(builder);

        var app = builder.Build();

        // 1. Read-only guard, first, so it applies to absolutely everything
        //    below it including the static-file middleware.
        app.Use(async (context, next) =>
        {
            // Applied to every answer this listener gives, including the refusal
            // below: the content types here are whatever a generator wrote, and
            // nothing served from a documentation port should ever be sniffed
            // into something executable.
            context.Response.Headers.XContentTypeOptions = "nosniff";
            context.Response.Headers["Referrer-Policy"] = "no-referrer";

            if (!ReadOnlyMethods.Contains(context.Request.Method, StringComparer.OrdinalIgnoreCase))
            {
                context.Response.StatusCode = StatusCodes.Status405MethodNotAllowed;
                context.Response.Headers.Allow = "GET, HEAD";
                context.Response.ContentType = "text/plain; charset=utf-8";
                await context.Response.WriteAsync(
                    "The Project CHAOS documentation listener is read-only. It serves generated documentation "
                  + "and has no API, no proxy and no write endpoints; the control plane is the gateway, on its "
                  + "own port.").ConfigureAwait(false);
                return;
            }

            await next(context).ConfigureAwait(false);
        });

        if (root.HasIndex)
        {
            var provider = new PhysicalFileProvider(root.Path!);
            var contentTypes = new FileExtensionContentTypeProvider();
            contentTypes.Mappings[".js"] = "text/javascript";
            contentTypes.Mappings[".mjs"] = "text/javascript";

            app.UseDefaultFiles(new DefaultFilesOptions { FileProvider = provider });
            app.UseStaticFiles(new StaticFileOptions
            {
                FileProvider = provider,
                ContentTypeProvider = contentTypes,

                // A generated documentation site is a closed set of file types.
                // Serving unknown ones would mean guessing at a content type for
                // whatever else ended up in the directory.
                ServeUnknownFileTypes = false,
                OnPrepareResponse = ApplyCachePolicy,
            });

            // Anything the static files did not answer: an explanation, never a
            // stack trace. 404 is right here — the site exists, this page does not.
            app.Run(async context =>
            {
                context.Response.StatusCode = StatusCodes.Status404NotFound;
                await WriteHtmlAsync(
                    context,
                    DocumentationPages.NotFound(context.Request.Path.Value ?? "/"),
                    DocumentationPages.StateNotFound).ConfigureAwait(false);
            });
        }
        else
        {
            // No site yet. EVERY path answers with the same page explaining how
            // to build it from Visual Studio, at HTTP 200: nothing is broken and
            // nothing is missing that a reader could fix by correcting the URL.
            var page = DocumentationPages.NotGenerated(root);
            app.Run(async context =>
                await WriteHtmlAsync(context, page, DocumentationPages.StateNotGenerated).ConfigureAwait(false));
        }

        return app;
    }

    /// <summary>
    /// The addresses a started documentation application actually bound.
    /// </summary>
    /// <param name="app">A started application.</param>
    /// <returns>The bound addresses, or an empty list when the server does not report any.</returns>
    /// <remarks>
    /// Read after start rather than assumed from configuration, because port 0
    /// means "whatever is free" and the answer is only known afterwards.
    /// </remarks>
    public static IReadOnlyList<string> BoundAddresses(WebApplication app)
    {
        ArgumentNullException.ThrowIfNull(app);

        var addresses = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>();
        return addresses is null ? [] : [.. addresses.Addresses];
    }

    private static async Task WriteHtmlAsync(HttpContext context, string html, string state)
    {
        context.Response.ContentType = "text/html; charset=utf-8";
        context.Response.Headers[DocumentationPages.StateHeader] = state;
        ApplyNoCache(context.Response);

        // HEAD must not carry a body; the framework drops it, but the headers
        // above are the useful part of the answer either way.
        await context.Response.WriteAsync(html).ConfigureAwait(false);
    }

    private static void ApplyCachePolicy(StaticFileResponseContext context)
    {
        // Same policy as the operator console: revalidate the things that change
        // when the product is upgraded. A wall display must not still be showing
        // last month's commissioning procedure.
        foreach (var extension in NoCacheExtensions)
        {
            if (context.File.Name.EndsWith(extension, StringComparison.OrdinalIgnoreCase))
            {
                ApplyNoCache(context.Context.Response);
                return;
            }
        }
    }

    private static void ApplyNoCache(HttpResponse response)
    {
        response.Headers.CacheControl = "no-cache, must-revalidate";
        response.Headers.Pragma = "no-cache";
    }
}
