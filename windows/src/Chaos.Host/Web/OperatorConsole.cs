using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.StaticFiles;
using Microsoft.Extensions.FileProviders;

namespace Chaos.Host.Web;

/// <summary>
/// Serves the built-in operator console and annunciator panel from this gateway.
/// </summary>
/// <remarks>
/// <para>
/// Mirrors what <c>src/homestead_twin/api/app.py</c> does today: the assets are
/// mounted at <c>/ui</c> and <c>/</c> serves <c>index.html</c>. The console
/// resolves its own API root from the request path, so it works identically at
/// <c>/</c> and <c>/ui/</c> without any rewriting here.
/// </para>
/// <para>
/// <b>Caching.</b> <c>.html</c>, <c>.js</c>, <c>.css</c> and <c>.json</c> are
/// served <c>no-cache, must-revalidate</c>. Not <c>no-store</c> — ETag
/// revalidation still saves the bytes — but a browser may never use a cached
/// copy without asking. After an upgrade the annunciator panel on the wall must
/// not still be running last month's JavaScript.
/// </para>
/// <para>
/// <b>When the assets are missing</b> the gateway answers <c>/</c> with a JSON
/// explanation and HTTP 200, as the FastAPI app does. The API is the control
/// path; a missing console is not a server fault and must not present as one.
/// </para>
/// </remarks>
public static class OperatorConsole
{
    /// <summary>The path the console assets are mounted at.</summary>
    public const string MountPath = "/ui";

    private static readonly string[] NoCacheExtensions = [".html", ".htm", ".js", ".mjs", ".css", ".json", ".map"];

    /// <summary>
    /// Registers the static-file middleware and the root endpoint.
    /// </summary>
    /// <param name="app">The application.</param>
    /// <param name="resolution">Where the console assets were found, or were not.</param>
    /// <returns>The application, for chaining.</returns>
    public static WebApplication UseOperatorConsole(this WebApplication app, WebRootResolution resolution)
    {
        ArgumentNullException.ThrowIfNull(app);
        ArgumentNullException.ThrowIfNull(resolution);

        if (resolution.Path is null)
        {
            MapMissingConsoleRoot(app, resolution);
            return app;
        }

        var provider = new PhysicalFileProvider(resolution.Path);
        var contentTypes = new FileExtensionContentTypeProvider();

        // The console ships ES modules; some environments still map .js to
        // application/javascript, which browsers accept for modules, but being
        // explicit removes a whole class of "the console is blank" reports.
        contentTypes.Mappings[".js"] = "text/javascript";
        contentTypes.Mappings[".mjs"] = "text/javascript";

        app.UseDefaultFiles(new DefaultFilesOptions
        {
            FileProvider = provider,
            RequestPath = MountPath,
        });

        app.UseStaticFiles(new StaticFileOptions
        {
            FileProvider = provider,
            RequestPath = MountPath,
            ContentTypeProvider = contentTypes,
            ServeUnknownFileTypes = false,
            OnPrepareResponse = ApplyCachePolicy,
        });

        // FastAPI's StaticFiles mount redirects /ui to /ui/; match that so a
        // bookmarked "/ui" behaves the same through the gateway.
        app.MapGet(MountPath, () => Results.Redirect($"{MountPath}/", permanent: false))
           .ExcludeFromDescription();

        MapConsoleRoot(app, resolution, provider);
        return app;
    }

    private static void MapConsoleRoot(WebApplication app, WebRootResolution resolution, PhysicalFileProvider provider)
    {
        app.MapGet("/", (HttpContext context) =>
        {
            var index = provider.GetFileInfo("index.html");
            if (!index.Exists || index.PhysicalPath is null)
            {
                return ConsoleUnavailable(resolution);
            }

            ApplyNoCache(context.Response);
            return Results.File(index.PhysicalPath, "text/html; charset=utf-8");
        }).ExcludeFromDescription();
    }

    private static void MapMissingConsoleRoot(WebApplication app, WebRootResolution resolution)
    {
        app.MapGet("/", () => ConsoleUnavailable(resolution)).ExcludeFromDescription();
    }

    private static IResult ConsoleUnavailable(WebRootResolution resolution) => Results.Json(
        new
        {
            status = "ok",
            detail = "Operator UI is not installed; the API is available at /api/v1.",
            docs = "/docs",
            health = "/health",
            webRoot = new
            {
                configured = resolution.Source == "configured",
                resolved = resolution.Path,
                searched = resolution.SearchedPaths,
            },
        },
        statusCode: StatusCodes.Status200OK);

    private static void ApplyCachePolicy(StaticFileResponseContext context)
    {
        var path = context.File.Name;
        foreach (var extension in NoCacheExtensions)
        {
            if (path.EndsWith(extension, StringComparison.OrdinalIgnoreCase))
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
