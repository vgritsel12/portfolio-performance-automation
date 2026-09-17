/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: EPL-1.0
 */
package name.abuchen.portfolio.cli;

import java.io.BufferedWriter;
import java.io.IOException;
import java.io.InputStream;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.stream.Collectors;

import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilderFactory;

import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.google.gson.JsonPrimitive;
import com.google.gson.JsonSerializer;

import name.abuchen.portfolio.model.Account;
import name.abuchen.portfolio.model.AccountTransaction;
import name.abuchen.portfolio.model.Classification;
import name.abuchen.portfolio.model.Client;
import name.abuchen.portfolio.model.ClientFactory;
import name.abuchen.portfolio.model.ConfigurationSet;
import name.abuchen.portfolio.model.ConfigurationSet.WellKnownConfigurationSets;
import name.abuchen.portfolio.model.LatestSecurityPrice;
import name.abuchen.portfolio.model.Portfolio;
import name.abuchen.portfolio.model.PortfolioTransaction;
import name.abuchen.portfolio.model.Security;
import name.abuchen.portfolio.model.SecurityPrice;
import name.abuchen.portfolio.model.Taxonomy;
import name.abuchen.portfolio.model.Transaction;
import name.abuchen.portfolio.money.CurrencyConverter;
import name.abuchen.portfolio.money.CurrencyConverterImpl;
import name.abuchen.portfolio.money.ExchangeRate;
import name.abuchen.portfolio.money.ExchangeRateProvider;
import name.abuchen.portfolio.money.ExchangeRateProviderFactory;
import name.abuchen.portfolio.money.ExchangeRateTimeSeries;
import name.abuchen.portfolio.money.Values;
import name.abuchen.portfolio.money.impl.EmptyExchangeRateTimeSeries;
import name.abuchen.portfolio.money.impl.ExchangeRateTimeSeriesImpl;
import name.abuchen.portfolio.snapshot.AccountSnapshot;
import name.abuchen.portfolio.snapshot.AssetCategory;
import name.abuchen.portfolio.snapshot.AssetPosition;
import name.abuchen.portfolio.snapshot.ClientSnapshot;
import name.abuchen.portfolio.snapshot.GroupByTaxonomy;
import name.abuchen.portfolio.snapshot.PerformanceIndex;
import name.abuchen.portfolio.snapshot.PortfolioSnapshot;
import name.abuchen.portfolio.snapshot.ReportingPeriod;
import name.abuchen.portfolio.snapshot.SecurityPosition;
import name.abuchen.portfolio.snapshot.filter.PortfolioClientFilter;
import name.abuchen.portfolio.snapshot.filter.ReadOnlyAccount;
import name.abuchen.portfolio.util.Interval;

/**
 * Minimal read-only exporter used by the local prototype.
 */
public final class PortfolioPrototypeCli
{
    private static final String ENGINE_COMMIT = "2f3917512f4d042cc4dd2abcf24ae62d665f9b18"; //$NON-NLS-1$
    private static final String ENGINE_PROJECT_VERSION = "0.86.1-SNAPSHOT"; //$NON-NLS-1$
    private static final String CLIENT_FILTER_PREFIX = "ClientFilter"; //$NON-NLS-1$
    private static final String FULL_XML_SCOPE = "FULL_XML"; //$NON-NLS-1$

    private static final Gson GSON = new GsonBuilder()
                    .registerTypeAdapter(LocalDate.class,
                                    (JsonSerializer<LocalDate>) (date, type, context) -> new JsonPrimitive(
                                                    date.toString()))
                    .serializeNulls().setPrettyPrinting().create();

    private PortfolioPrototypeCli()
    {
    }

    public static void main(String[] args)
    {
        int exitCode = new PortfolioPrototypeCli().execute(args);
        if (exitCode != 0)
            System.exit(exitCode);
    }

    private int execute(String[] args)
    {
        if (args.length < 1 || args.length > 3)
        {
            System.err.println("Usage: PortfolioPrototypeCli <input.xml> [output-directory] [comparison.xml]"); //$NON-NLS-1$
            return 64;
        }

        Path input = Path.of(args[0]).toAbsolutePath().normalize();
        Path output = args.length >= 2 ? Path.of(args[1]).toAbsolutePath().normalize()
                        : Path.of("output").toAbsolutePath().normalize(); //$NON-NLS-1$
        Path comparisonInput = args.length == 3 ? Path.of(args[2]).toAbsolutePath().normalize()
                        : findComparisonInput(input);

        try
        {
            Files.createDirectories(output);

            String requestedReportingCurrency = environmentCurrency("PP_REPORTING_CURRENCY"); //$NON-NLS-1$
            String dashboardCurrency = environmentCurrency("PP_DASHBOARD_CURRENCY"); //$NON-NLS-1$
            String clientScope = environmentClientScope();
            Path fxCache = environmentPath("PP_ECB_CACHE"); //$NON-NLS-1$

            SourceCalculation main = calculateSource(input, true, requestedReportingCurrency,
                            dashboardCurrency, clientScope, fxCache);
            SourceCalculation comparison = comparisonInput != null && Files.isRegularFile(comparisonInput)
                            ? calculateSource(comparisonInput, false, requestedReportingCurrency,
                                            dashboardCurrency, clientScope, fxCache)
                            : null;

            writeReport(output.resolve("report.json"), main); //$NON-NLS-1$
            writeSeries(output.resolve("performance_series.csv"), main.allTime); //$NON-NLS-1$
            writeDiagnostics(output.resolve("calculation_diagnostics.json"), main); //$NON-NLS-1$
            writeComparison(output.resolve("xml_version_comparison.md"), main, comparison); //$NON-NLS-1$
            String overallStatus = writeValidation(output.resolve("validation_report.md"), main); //$NON-NLS-1$

            System.out.println(overallStatus);
            return main.hasAnyCalculatedMetric() && !main.fx.hasFatalFallback() ? 0 : 2;
        }
        catch (Exception e)
        {
            System.err.println("NOT_YET_CALCULABLE: " + describe(e)); //$NON-NLS-1$
            return 2;
        }
    }

    private static Path findComparisonInput(Path input)
    {
        Path name = input.getFileName();
        if (name == null || input.getParent() == null)
            return null;

        if ("Portfolio data.xml".equals(name.toString())) //$NON-NLS-1$
            return input.resolveSibling("Portfolio data.backup.xml"); //$NON-NLS-1$
        if ("Portfolio data.backup.xml".equals(name.toString())) //$NON-NLS-1$
            return input.resolveSibling("Portfolio data.xml"); //$NON-NLS-1$
        return null;
    }

    private static String environmentCurrency(String name) throws IOException
    {
        String value = System.getenv(name);
        if (value == null || value.isBlank())
            return null;
        String normalized = value.trim().toUpperCase(Locale.ROOT);
        if (!normalized.matches("[A-Z]{3}")) //$NON-NLS-1$
            throw new IOException(name + " must be a 3-letter ISO currency code"); //$NON-NLS-1$
        return normalized;
    }

    private static Path environmentPath(String name)
    {
        String value = System.getenv(name);
        return value == null || value.isBlank() ? null : Path.of(value).toAbsolutePath().normalize();
    }

    private static String environmentClientScope() throws IOException
    {
        String value = System.getenv("PP_CLIENT_SCOPE"); //$NON-NLS-1$
        if (value == null || value.isBlank())
            return null;
        String raw = value.trim();
        String normalized = raw.toUpperCase(Locale.ROOT);
        if (!FULL_XML_SCOPE.equals(normalized))
        {
            if (!raw.matches("[0-9a-fA-F-]{8,64}")) //$NON-NLS-1$
                throw new IOException("PP_CLIENT_SCOPE must be FULL_XML or a saved filter UUID"); //$NON-NLS-1$
            return raw;
        }
        return FULL_XML_SCOPE;
    }

    private static SourceCalculation calculateSource(Path input, boolean detailed, String requestedReportingCurrency,
                    String requestedDashboardCurrency, String clientScope, Path fxCache)
    {
        SourceCalculation calculation = new SourceCalculation(input);

        try
        {
            if (!Files.isRegularFile(input) || !Files.isReadable(input))
                throw new IOException("Input XML is not a readable regular file: " + input); //$NON-NLS-1$

            calculation.sha256Before = sha256(input);

            try (InputStream stream = Files.newInputStream(input))
            {
                calculation.client = ClientFactory.load(stream);
            }

            calculation.modelVersion = Integer.valueOf(calculation.client.getFileVersionAfterRead());
            calculation.reportingCurrency = requestedReportingCurrency != null ? requestedReportingCurrency
                            : calculation.client.getBaseCurrency();
            calculation.reportDate = determineReportDate(calculation.client);
            calculation.stats = EntityStats.from(calculation.client);

            String dashboardCurrency = requestedDashboardCurrency != null ? requestedDashboardCurrency
                            : calculation.reportingCurrency;
            DashboardSelection selection = DashboardSelection.read(input, dashboardCurrency);
            calculation.fx = new FxDiagnostics();
            ExchangeRateProviderFactory exchangeRates = fxCache == null
                            ? new ExchangeRateProviderFactory(calculation.client)
                            : new SnapshotExchangeRateProviderFactory(calculation.client,
                                            loadSnapshotFx(calculation.client, fxCache));
            CurrencyConverter converter = new DiagnosticCurrencyConverter(
                            new CurrencyConverterImpl(exchangeRates, calculation.reportingCurrency), exchangeRates,
                            calculation.fx);

            calculation.allTime = calculatePeriod(calculation.client, converter, selection.allTime(),
                            calculation.reportDate, calculation, clientScope);
            calculation.currentYear = calculatePeriod(calculation.client, converter, selection.currentYear(),
                            calculation.reportDate, calculation, clientScope);

            if (calculation.allTime.index != null)
            {
                long[] totals = calculation.allTime.index.getTotals();
                calculation.portfolioMarketValue = amount(totals[totals.length - 1]);
            }

            calculation.portfolioOverview = buildPortfolioOverview(calculation, converter);
            buildBoundaryDiagnostics(calculation, converter);

            if (detailed && calculation.allTime.filter != null && calculation.allTime.effectiveInterval != null)
                scanPriceDiagnostics(calculation, converter);
        }
        catch (Exception e)
        {
            calculation.fatalError = describe(e);
        }
        finally
        {
            try
            {
                if (Files.isRegularFile(input))
                    calculation.sha256After = sha256(input);
            }
            catch (IOException e)
            {
                calculation.postCalculationErrors.add(describe(e));
            }

            if (calculation.sha256Before != null && !calculation.sha256Before.equals(calculation.sha256After))
            {
                calculation.postCalculationErrors.add("SOURCE_XML_SHA256_CHANGED"); //$NON-NLS-1$
                if (calculation.fatalError == null)
                    calculation.fatalError = "Source XML SHA-256 changed during calculation"; //$NON-NLS-1$
            }
        }

        return calculation;
    }

    private static ExchangeRateProvider loadSnapshotFx(Client client, Path cache) throws IOException
    {
        List<Path> cacheFiles;
        if (Files.isRegularFile(cache) && Files.isReadable(cache))
        {
            cacheFiles = List.of(cache);
        }
        else if (Files.isDirectory(cache) && Files.isReadable(cache))
        {
            try (var files = Files.list(cache))
            {
                cacheFiles = files.filter(Files::isRegularFile)
                                .filter(path -> path.getFileName().toString().matches("ecb-eur-[a-z]{3}\\.json")) //$NON-NLS-1$
                                .sorted().toList();
            }
        }
        else
        {
            throw new IOException("ECB cache is not a readable file or directory"); //$NON-NLS-1$
        }
        if (cacheFiles.isEmpty())
            throw new IOException("ECB cache directory contains no official series"); //$NON-NLS-1$

        try
        {
            SnapshotExchangeRateProvider provider = new SnapshotExchangeRateProvider();
            Set<String> quoteCurrencies = new LinkedHashSet<>();
            for (Path cacheFile : cacheFiles)
            {
                JsonObject root = JsonParser
                                .parseString(Files.readString(cacheFile, StandardCharsets.UTF_8)).getAsJsonObject();
                String quoteCurrency = root.get("quote_currency").getAsString(); //$NON-NLS-1$
                String expectedSeries = "D." + quoteCurrency + ".EUR.SP00.A"; //$NON-NLS-1$ //$NON-NLS-2$
                if (!"European Central Bank".equals(root.get("provider").getAsString()) //$NON-NLS-1$ //$NON-NLS-2$
                                || !expectedSeries.equals(root.get("series_key").getAsString()) //$NON-NLS-1$
                                || !"EUR".equals(root.get("base_currency").getAsString()) //$NON-NLS-1$ //$NON-NLS-2$
                                || !quoteCurrency.matches("[A-Z]{3}") //$NON-NLS-1$
                                || !quoteCurrencies.add(quoteCurrency))
                    throw new IOException("ECB cache metadata is invalid"); //$NON-NLS-1$

                ExchangeRateTimeSeriesImpl series = new ExchangeRateTimeSeriesImpl(provider, "EUR", //$NON-NLS-1$
                                quoteCurrency);
                root.getAsJsonArray("observations").forEach(item -> {
                    JsonObject row = item.getAsJsonObject();
                    LocalDate day = LocalDate.parse(row.get("date").getAsString()); //$NON-NLS-1$
                    BigDecimal rate = new BigDecimal(row.get("rate").getAsString()); //$NON-NLS-1$
                    if (rate.signum() <= 0)
                        throw new IllegalArgumentException("ECB rate must be positive"); //$NON-NLS-1$
                    series.addRate(new ExchangeRate(day, rate));
                });
                if (series.getRates().isEmpty())
                    throw new IOException("ECB cache has no observations"); //$NON-NLS-1$
                provider.add(series);
            }
            return provider;
        }
        catch (IOException e)
        {
            throw e;
        }
        catch (RuntimeException e)
        {
            throw new IOException("ECB cache cannot be parsed", e); //$NON-NLS-1$
        }
    }

    private static final class SnapshotExchangeRateProvider implements ExchangeRateProvider
    {
        private final List<ExchangeRateTimeSeries> series = new ArrayList<>();

        void add(ExchangeRateTimeSeries item)
        {
            series.add(item);
        }

        @Override
        public String getName()
        {
            return "European Central Bank snapshot"; //$NON-NLS-1$
        }

        @Override
        public List<ExchangeRateTimeSeries> getAvailableTimeSeries(Client client)
        {
            return new ArrayList<>(series);
        }
    }

    private static final class SnapshotExchangeRateProviderFactory extends ExchangeRateProviderFactory
    {
        private final ExchangeRateProvider snapshot;

        SnapshotExchangeRateProviderFactory(Client client, ExchangeRateProvider snapshot)
        {
            super(client);
            this.snapshot = snapshot;
        }

        @Override
        public List<ExchangeRateTimeSeries> getAvailableTimeSeries()
        {
            List<ExchangeRateTimeSeries> answer = new ArrayList<>(snapshot.getAvailableTimeSeries(null));
            answer.addAll(super.getAvailableTimeSeries());
            return answer;
        }
    }

    private static LocalDate determineReportDate(Client client) throws IOException
    {
        LocalDate latest = null;

        for (Account account : client.getAccounts())
        {
            for (AccountTransaction transaction : account.getTransactions())
                latest = max(latest, transaction.getDateTime().toLocalDate());
        }

        for (Portfolio portfolio : client.getPortfolios())
        {
            for (PortfolioTransaction transaction : portfolio.getTransactions())
                latest = max(latest, transaction.getDateTime().toLocalDate());
        }

        for (Security security : client.getSecurities())
        {
            for (SecurityPrice price : security.getPrices())
                latest = max(latest, price.getDate());
            if (security.getLatest() != null)
                latest = max(latest, security.getLatest().getDate());
        }

        if (latest == null)
            throw new IOException("Could not determine report date from transactions or prices"); //$NON-NLS-1$
        return latest;
    }

    private static LocalDate max(LocalDate left, LocalDate right)
    {
        if (right == null)
            return left;
        return left == null || right.isAfter(left) ? right : left;
    }

    private static PeriodResult calculatePeriod(Client client, CurrencyConverter converter, PeriodSelection selection,
                    LocalDate reportDate, SourceCalculation calculation, String clientScope)
    {
        PeriodResult result = new PeriodResult();
        result.periodCode = selection.code();
        result.requestedInterval = selection.interval();

        LocalDate effectiveEnd = selection.interval().getEnd().isAfter(reportDate) ? reportDate
                        : selection.interval().getEnd();
        LocalDate effectiveStart = selection.interval().getStart().isAfter(effectiveEnd) ? effectiveEnd
                        : selection.interval().getStart();
        result.effectiveInterval = Interval.of(effectiveStart, effectiveEnd);

        try
        {
            result.filter = FULL_XML_SCOPE.equals(clientScope) ? resolveFullClient(client)
                            : resolveFilter(client, clientScope != null ? clientScope : selection.filterUUID());
            registerCurrencies(result.filter.filteredClient(), converter.getTermCurrency(), calculation.fx);

            List<Exception> warnings = new ArrayList<>();
            PerformanceIndex index = PerformanceIndex.forClient(result.filter.filteredClient(), converter,
                            result.effectiveInterval, warnings);

            result.index = index;
            result.cumulativeTtwror = finite(index.getFinalAccumulatedPercentage());
            result.annualizedTtwror = finite(index.getFinalAccumulatedAnnualizedPercentage());
            result.irr = finite(index.getPerformanceIRR());

            long[] totals = index.getTotals();
            long[] profit = index.calculateDelta();
            double[] cumulative = index.getAccumulatedPercentage();
            result.startValuation = amount(totals[0]);
            result.endValuation = amount(totals[totals.length - 1]);
            result.endingTotalMarketValue = result.endValuation;
            result.absoluteChange = amount(totals[totals.length - 1] - totals[0]);
            result.profit = amount(profit[profit.length - 1]);
            result.startCumulativeTtwror = finite(cumulative[0]);
            result.endCumulativeTtwror = finite(cumulative[cumulative.length - 1]);
            result.warnings = warnings.stream().map(PortfolioPrototypeCli::describe).toList();
            calculation.engineWarnings.addAll(result.warnings);
        }
        catch (Exception e)
        {
            result.error = describe(e);
            calculation.periodErrors.add(selection.code() + ": " + result.error); //$NON-NLS-1$
        }

        return result;
    }

    private static ResolvedFilter resolveFilter(Client client, String filterUUID) throws IOException
    {
        if (!client.getSettings().hasConfigurationSet(WellKnownConfigurationSets.CLIENT_FILTER_DEFINITIONS.getKey()))
            throw new IOException("XML has no saved client-filter-definitions"); //$NON-NLS-1$

        ConfigurationSet definitions = client.getSettings()
                        .getConfigurationSet(WellKnownConfigurationSets.CLIENT_FILTER_DEFINITIONS);
        ConfigurationSet.Configuration configuration = definitions.lookup(filterUUID)
                        .orElseThrow(() -> new IOException("Saved client filter was not found: " + filterUUID)); //$NON-NLS-1$

        Map<String, Object> byUUID = new HashMap<>();
        client.getAccounts().forEach(account -> byUUID.put(account.getUUID(), account));
        client.getPortfolios().forEach(portfolio -> byUUID.put(portfolio.getUUID(), portfolio));

        List<Account> accounts = new ArrayList<>();
        List<Portfolio> portfolios = new ArrayList<>();
        Map<Object, Integer> weights = new HashMap<>();
        List<FilterComponent> components = new ArrayList<>();

        for (String rawToken : configuration.getData().split(",")) //$NON-NLS-1$
        {
            String token = rawToken.trim();
            String uuid = token;
            int weight = Classification.ONE_HUNDRED_PERCENT;
            int separator = token.indexOf(':');
            if (separator >= 0)
            {
                uuid = token.substring(0, separator);
                try
                {
                    weight = Integer.parseInt(token.substring(separator + 1).trim());
                }
                catch (NumberFormatException e)
                {
                    throw new IOException("Invalid client-filter weight: " + token, e); //$NON-NLS-1$
                }
                if (weight < 1 || weight > Classification.ONE_HUNDRED_PERCENT)
                    throw new IOException("Client-filter weight is outside the supported range: " + token); //$NON-NLS-1$
            }

            Object element = byUUID.get(uuid);
            if (element instanceof Account account)
            {
                accounts.add(account);
                components.add(new FilterComponent("ACCOUNT", uuid, account.getName(), account.getCurrencyCode(), //$NON-NLS-1$
                                weight));
            }
            else if (element instanceof Portfolio portfolio)
            {
                portfolios.add(portfolio);
                String currency = portfolio.getReferenceAccount() != null
                                ? portfolio.getReferenceAccount().getCurrencyCode()
                                : null;
                components.add(new FilterComponent("PORTFOLIO", uuid, portfolio.getName(), currency, weight)); //$NON-NLS-1$
            }
            else
            {
                throw new IOException("Client-filter member was not found: " + uuid); //$NON-NLS-1$
            }

            if (weight < Classification.ONE_HUNDRED_PERCENT)
                weights.put(element, Integer.valueOf(weight));
        }

        if (accounts.isEmpty() && portfolios.isEmpty())
            throw new IOException("Saved client filter has no resolvable members: " + filterUUID); //$NON-NLS-1$

        return resolvedFilter("CLIENT_FILTER", filterUUID, configuration.getName(), accounts, portfolios, weights, //$NON-NLS-1$
                        components, client);
    }

    private static ResolvedFilter resolveFullClient(Client client) throws IOException
    {
        List<Portfolio> portfolios = client.getPortfolios().stream().sorted(Comparator.comparing(Portfolio::getUUID))
                        .toList();
        Set<String> referenceAccounts = portfolios.stream().map(Portfolio::getReferenceAccount).filter(Objects::nonNull)
                        .map(Account::getUUID).collect(Collectors.toCollection(TreeSet::new));
        List<Account> accounts = client.getAccounts().stream().filter(account -> !referenceAccounts.contains(account.getUUID()))
                        .sorted(Comparator.comparing(Account::getUUID)).toList();
        List<FilterComponent> components = new ArrayList<>();
        accounts.forEach(account -> components.add(new FilterComponent("ACCOUNT", account.getUUID(), account.getName(), //$NON-NLS-1$
                        account.getCurrencyCode(), Classification.ONE_HUNDRED_PERCENT)));
        portfolios.forEach(portfolio -> components.add(new FilterComponent("PORTFOLIO", portfolio.getUUID(), //$NON-NLS-1$
                        portfolio.getName(), portfolio.getReferenceAccount() != null
                                        ? portfolio.getReferenceAccount().getCurrencyCode()
                                        : null,
                        Classification.ONE_HUNDRED_PERCENT)));
        if (accounts.isEmpty() && portfolios.isEmpty())
            throw new IOException("Full XML client scope has no accounts or portfolios"); //$NON-NLS-1$
        return resolvedFilter("FULL_CLIENT", FULL_XML_SCOPE, "Full XML client", accounts, portfolios, Map.of(), //$NON-NLS-1$ //$NON-NLS-2$
                        components, client);
    }

    private static ResolvedFilter resolvedFilter(String type, String uuid, String name, List<Account> accounts,
                    List<Portfolio> portfolios, Map<Object, Integer> weights, List<FilterComponent> components,
                    Client client)
    {
        Client filtered = new PortfolioClientFilter(portfolios, accounts, weights).filter(client);
        Set<String> accountClosure = new TreeSet<>();
        accounts.forEach(account -> accountClosure.add(account.getUUID()));
        portfolios.stream().map(Portfolio::getReferenceAccount).filter(Objects::nonNull).map(Account::getUUID)
                        .forEach(accountClosure::add);
        List<String> portfolioClosure = portfolios.stream().map(Portfolio::getUUID).sorted().toList();
        return new ResolvedFilter(type, uuid, name, components, List.copyOf(accountClosure), portfolioClosure,
                        filtered);
    }

    private static void registerCurrencies(Client client, String termCurrency, FxDiagnostics diagnostics)
    {
        client.getAccounts().forEach(account -> diagnostics.registerPair(account.getCurrencyCode(), termCurrency));
        client.getPortfolios().stream().flatMap(portfolio -> portfolio.getTransactions().stream())
                        .map(PortfolioTransaction::getSecurity).filter(Objects::nonNull).map(Security::getCurrencyCode)
                        .distinct().forEach(currency -> diagnostics.registerPair(currency, termCurrency));
    }

    private static PortfolioOverview buildPortfolioOverview(SourceCalculation calculation, CurrencyConverter converter)
                    throws IOException
    {
        if (calculation.allTime.filter == null || calculation.allTime.effectiveInterval == null)
            throw new IOException("Portfolio overview requires the resolved all-time client filter and interval"); //$NON-NLS-1$

        LocalDate date = calculation.allTime.effectiveInterval.getEnd();
        Client scopedClient = calculation.allTime.filter.filteredClient();
        ClientSnapshot snapshot = ClientSnapshot.create(scopedClient, converter, date);
        Double snapshotTotal = amount(snapshot.getMonetaryAssets().getAmount());
        if (calculation.portfolioMarketValue != null
                        && Math.abs(snapshotTotal.doubleValue() - calculation.portfolioMarketValue.doubleValue()) > 0.01d)
        {
            throw new IOException("Snapshot total does not match PerformanceIndex portfolio market value: " //$NON-NLS-1$
                            + snapshotTotal + " != " + calculation.portfolioMarketValue); //$NON-NLS-1$
        }

        List<AssetPosition> positions = snapshot.getAssetPositions().toList();

        long cashValue = 0L;
        double cashWeight = 0d;
        for (AssetPosition position : positions)
        {
            if (position.getSecurity() == null)
            {
                cashValue += position.getValuation().getAmount();
                cashWeight += position.getShare();
            }
        }

        List<CashAccountOverview> cashAccounts = snapshot.getAccounts().stream().map(account -> {
            Account snapshotAccount = account.getAccount();
            Account sourceAccount = snapshotAccount instanceof ReadOnlyAccount readOnly ? readOnly.unwrap()
                            : snapshotAccount;
            String originalCurrency = account.getUnconvertedFunds().getCurrencyCode();
            String reportingCurrency = account.getFunds().getCurrencyCode();
            if (!calculation.reportingCurrency.equals(reportingCurrency))
                throw new IllegalStateException("Cash account reporting currency differs from the report currency"); //$NON-NLS-1$
            return new CashAccountOverview(sourceAccount.getUUID(), originalCurrency,
                            amount(account.getUnconvertedFunds().getAmount()), reportingCurrency,
                            amount(account.getFunds().getAmount()), account.getFunds().getAmount());
        }).sorted(Comparator.comparing(CashAccountOverview::accountUUID)).toList();
        long accountCashValue = cashAccounts.stream().mapToLong(CashAccountOverview::reportingMinorUnits).sum();
        if (Math.abs(accountCashValue - cashValue) > 1L)
            throw new IOException("Cash account rows do not reconcile to aggregate cash: " + accountCashValue //$NON-NLS-1$
                            + " != " + cashValue); //$NON-NLS-1$
        Set<String> cashAccountUUIDs = cashAccounts.stream().map(CashAccountOverview::accountUUID)
                        .collect(Collectors.toCollection(TreeSet::new));
        if (!cashAccountUUIDs.equals(new TreeSet<>(calculation.allTime.filter.accountClosureUUIDs())))
            throw new IOException("Cash account row count does not match the full account scope: " //$NON-NLS-1$
                            + cashAccounts.size() + " != " + calculation.allTime.filter.accountClosureUUIDs().size()); //$NON-NLS-1$

        Taxonomy taxonomy = assetClassTaxonomy(calculation.client);
        GroupByTaxonomy grouped = snapshot.groupByTaxonomy(taxonomy);
        List<AllocationOverview> allocation = new ArrayList<>();
        Map<String, Set<String>> assetClassesBySecurity = new LinkedHashMap<>();

        for (AssetCategory category : grouped.asList())
        {
            long marketValue = 0L;
            double weight = 0d;
            boolean hasSecurity = false;
            String assetClass = assetClassName(category);

            for (AssetPosition position : category.getPositions())
            {
                Security security = position.getSecurity();
                if (security == null)
                    continue;

                hasSecurity = true;
                marketValue += position.getValuation().getAmount();
                weight += position.getShare();
                assetClassesBySecurity.computeIfAbsent(security.getUUID(), key -> new LinkedHashSet<>())
                                .add(assetClass);
            }

            if (hasSecurity)
                allocation.add(new AllocationOverview(assetClass, amount(marketValue), finiteWeight(weight)));
        }

        allocation.sort(Comparator.comparingDouble((AllocationOverview item) -> item.marketValue()).reversed()
                        .thenComparing(AllocationOverview::name, String.CASE_INSENSITIVE_ORDER));

        List<HoldingOverview> holdings = positions.stream().filter(position -> position.getSecurity() != null)
                        .map(position -> {
                            Security security = position.getSecurity();
                            Set<String> classes = assetClassesBySecurity.get(security.getUUID());
                            String assetClass = classes == null || classes.isEmpty() ? "Unclassified" //$NON-NLS-1$
                                            : classes.stream().sorted(String.CASE_INSENSITIVE_ORDER)
                                                            .collect(Collectors.joining(" / ")); //$NON-NLS-1$
                            return new HoldingOverview(security.getUUID(), security.getName(),
                                            amount(position.getValuation().getAmount()),
                                            finiteWeight(position.getShare()), security.getCurrencyCode(), assetClass);
                        }).sorted(Comparator.comparingDouble((HoldingOverview item) -> item.marketValue()).reversed()
                                        .thenComparing(HoldingOverview::name, String.CASE_INSENSITIVE_ORDER))
                        .toList();

        ScopeOverview scope = new ScopeOverview(calculation.allTime.filter.type(),
                        calculation.allTime.filter.accountClosureUUIDs(),
                        calculation.allTime.filter.portfolioClosureUUIDs());
        return new PortfolioOverview(new CashOverview(amount(cashValue), finiteWeight(cashWeight), cashAccounts), scope,
                        allocation, holdings, holdings.stream().limit(5).toList());
    }

    private static Taxonomy assetClassTaxonomy(Client client)
    {
        if (client == null)
            return null;

        for (Taxonomy taxonomy : client.getTaxonomies())
        {
            if (taxonomy.getRoot() != null && "assetclasses".equalsIgnoreCase(taxonomy.getRoot().getKey())) //$NON-NLS-1$
                return taxonomy;
        }

        for (Taxonomy taxonomy : client.getTaxonomies())
        {
            if (taxonomy.getDimensions() != null && taxonomy.getDimensions().stream()
                            .anyMatch(dimension -> "Asset Class".equalsIgnoreCase(dimension))) //$NON-NLS-1$
                return taxonomy;
        }

        return null;
    }

    private static String assetClassName(AssetCategory category)
    {
        Classification classification = category.getClassification();
        if (classification == null || Classification.UNASSIGNED_ID.equals(classification.getId())
                        || classification.getName() == null || classification.getName().isBlank())
            return "Unclassified"; //$NON-NLS-1$
        return classification.getName();
    }

    private static Double finiteWeight(double weight)
    {
        if (!Double.isFinite(weight))
            throw new IllegalStateException("Snapshot returned a non-finite portfolio weight"); //$NON-NLS-1$
        return Double.valueOf(weight);
    }

    private static void buildBoundaryDiagnostics(SourceCalculation calculation, CurrencyConverter converter)
    {
        if (calculation.allTime.index == null || calculation.currentYear.index == null
                        || calculation.currentYear.filter == null)
            return;

        try
        {
            LocalDate boundaryDate = calculation.currentYear.effectiveInterval.getStart();
            int boundaryIndex = indexOf(calculation.allTime.index.getDates(), boundaryDate);
            if (boundaryIndex < 0)
                throw new IOException("Boundary date is not present in all-time PerformanceIndex: " + boundaryDate); //$NON-NLS-1$

            PerformanceIndex allTime = calculation.allTime.index;
            long[] allTimeProfit = allTime.calculateDelta();
            double[] allTimeCumulative = allTime.getAccumulatedPercentage();
            int endIndex = allTime.getDates().length - 1;

            BoundaryDiagnostics boundary = new BoundaryDiagnostics();
            boundary.date = boundaryDate;
            boundary.totalPortfolioValuation = amount(allTime.getTotals()[boundaryIndex]);
            boundary.allTimeCumulativeTtwrorAtStart = finite(allTimeCumulative[boundaryIndex]);
            boundary.allTimeCumulativeTtwrorAtEnd = finite(allTimeCumulative[endIndex]);
            boundary.allTimeProfitAtStart = amount(allTimeProfit[boundaryIndex]);
            boundary.allTimeProfitAtEnd = amount(allTimeProfit[endIndex]);
            boundary.ttwrorFromBoundaryIdentity = Double
                            .valueOf((1d + boundary.allTimeCumulativeTtwrorAtEnd.doubleValue())
                                            / (1d + boundary.allTimeCumulativeTtwrorAtStart.doubleValue()) - 1d);
            boundary.ttwrorFromCurrentYearIndex = calculation.currentYear.cumulativeTtwror;
            boundary.ttwrorIdentityDifference = difference(boundary.ttwrorFromBoundaryIdentity,
                            boundary.ttwrorFromCurrentYearIndex);
            boundary.profitFromBoundaryIdentity = Double
                            .valueOf(boundary.allTimeProfitAtEnd.doubleValue() - boundary.allTimeProfitAtStart.doubleValue());
            boundary.profitFromCurrentYearIndex = calculation.currentYear.profit;
            boundary.profitIdentityDifference = difference(boundary.profitFromBoundaryIdentity,
                            boundary.profitFromCurrentYearIndex);

            Client filteredClient = calculation.currentYear.filter.filteredClient();
            ClientSnapshot snapshot = ClientSnapshot.create(filteredClient, converter, boundaryDate);
            boundary.snapshotTotal = amount(snapshot.getMonetaryAssets().getAmount());
            boundary.accounts = accountDiagnostics(snapshot);
            boundary.portfolios = portfolioDiagnostics(snapshot, converter, boundaryDate);
            boundary.appliedSecurityPrices = appliedPriceDiagnostics(snapshot, boundaryDate);
            boundary.transactions = transactionDiagnostics(filteredClient, boundaryDate);
            boundary.dataSeriesType = calculation.currentYear.filter.type();
            boundary.filterUUID = calculation.currentYear.filter.uuid();
            boundary.filterComponents = calculation.currentYear.filter.components();
            calculation.boundary = boundary;
        }
        catch (Exception e)
        {
            calculation.boundaryError = describe(e);
        }
    }

    private static int indexOf(LocalDate[] dates, LocalDate date)
    {
        for (int ii = 0; ii < dates.length; ii++)
        {
            if (dates[ii].equals(date))
                return ii;
        }
        return -1;
    }

    private static List<AccountDiagnostic> accountDiagnostics(ClientSnapshot snapshot)
    {
        List<AccountDiagnostic> answer = new ArrayList<>();
        for (AccountSnapshot account : snapshot.getAccounts())
        {
            answer.add(new AccountDiagnostic(account.getAccount().getUUID(), account.getAccount().getName(),
                            account.getUnconvertedFunds().getCurrencyCode(),
                            amount(account.getUnconvertedFunds().getAmount()), account.getFunds().getCurrencyCode(),
                            amount(account.getFunds().getAmount())));
        }
        return answer;
    }

    private static List<PortfolioDiagnostic> portfolioDiagnostics(ClientSnapshot snapshot, CurrencyConverter converter,
                    LocalDate date)
    {
        List<PortfolioDiagnostic> answer = new ArrayList<>();
        for (PortfolioSnapshot portfolio : snapshot.getPortfolios())
        {
            List<SecurityDiagnostic> securities = new ArrayList<>();
            for (SecurityPosition position : portfolio.getPositions())
            {
                Security security = position.getSecurity();
                SecurityPrice price = position.getPrice();
                Double unconverted = amount(position.calculateValue().getAmount());
                Double converted = amount(converter.convert(date, position.calculateValue()).getAmount());
                securities.add(new SecurityDiagnostic(security.getUUID(), security.getName(), security.getCurrencyCode(),
                                Double.valueOf(position.getShares() / Values.Share.divider()), price.getDate(),
                                Double.valueOf(price.getValue() / Values.Quote.divider()), unconverted, converted));
            }
            answer.add(new PortfolioDiagnostic(portfolio.unwrapPortfolio().getUUID(),
                            portfolio.unwrapPortfolio().getName(), amount(portfolio.getValue().getAmount()), securities));
        }
        return answer;
    }

    private static List<AppliedPriceDiagnostic> appliedPriceDiagnostics(ClientSnapshot snapshot, LocalDate date)
    {
        List<AppliedPriceDiagnostic> answer = new ArrayList<>();
        for (PortfolioSnapshot portfolio : snapshot.getPortfolios())
        {
            for (SecurityPosition position : portfolio.getPositions())
            {
                Security security = position.getSecurity();
                SecurityPrice raw = security.getSecurityPrice(date);
                SecurityPrice applied = position.getPrice();
                answer.add(new AppliedPriceDiagnostic(security.getUUID(), security.getName(), date, raw.getDate(),
                                Double.valueOf(raw.getValue() / Values.Quote.divider()), applied.getDate(),
                                Double.valueOf(applied.getValue() / Values.Quote.divider()),
                                classifyPriceFallback(date, raw, applied)));
            }
        }
        return answer;
    }

    private static Map<LocalDate, List<TransactionDiagnostic>> transactionDiagnostics(Client client,
                    LocalDate boundaryDate)
    {
        Map<LocalDate, List<TransactionDiagnostic>> answer = new LinkedHashMap<>();
        List.of(boundaryDate.minusDays(1), boundaryDate, boundaryDate.plusDays(1), boundaryDate.plusDays(2))
                        .forEach(date -> answer.put(date, new ArrayList<>()));

        for (Account account : client.getAccounts())
        {
            for (AccountTransaction transaction : account.getTransactions())
            {
                LocalDate date = transaction.getDateTime().toLocalDate();
                if (answer.containsKey(date))
                    answer.get(date).add(transactionDiagnostic("ACCOUNT", account.getUUID(), account.getName(), //$NON-NLS-1$
                                    transaction, transaction.getType().name()));
            }
        }

        for (Portfolio portfolio : client.getPortfolios())
        {
            for (PortfolioTransaction transaction : portfolio.getTransactions())
            {
                LocalDate date = transaction.getDateTime().toLocalDate();
                if (answer.containsKey(date))
                    answer.get(date).add(transactionDiagnostic("PORTFOLIO", portfolio.getUUID(), portfolio.getName(), //$NON-NLS-1$
                                    transaction, transaction.getType().name()));
            }
        }

        answer.values().forEach(list -> list.sort(Comparator.comparing(TransactionDiagnostic::dateTime)
                        .thenComparing(TransactionDiagnostic::uuid)));
        return answer;
    }

    private static TransactionDiagnostic transactionDiagnostic(String ownerType, String ownerUUID, String ownerName,
                    Transaction transaction, String type)
    {
        Security security = transaction.getSecurity();
        return new TransactionDiagnostic(ownerType, ownerUUID, ownerName, transaction.getUUID(),
                        transaction.getDateTime().toString(), type, transaction.getCurrencyCode(),
                        amount(transaction.getAmount()), Double.valueOf(transaction.getShares() / Values.Share.divider()),
                        security != null ? security.getUUID() : null, security != null ? security.getName() : null);
    }

    private static void scanPriceDiagnostics(SourceCalculation calculation, CurrencyConverter converter)
    {
        try
        {
            Client filteredClient = calculation.allTime.filter.filteredClient();
            Interval interval = calculation.allTime.effectiveInterval;
            Set<String> seenFallback = new LinkedHashSet<>();
            Set<String> seenMissing = new LinkedHashSet<>();

            LocalDate date = interval.getStart();
            while (!date.isAfter(interval.getEnd()))
            {
                ClientSnapshot snapshot = ClientSnapshot.create(filteredClient, converter, date);
                for (PortfolioSnapshot portfolio : snapshot.getPortfolios())
                {
                    for (SecurityPosition position : portfolio.getPositions())
                    {
                        Security security = position.getSecurity();
                        SecurityPrice raw = security.getSecurityPrice(date);
                        SecurityPrice applied = position.getPrice();
                        String type = classifyPriceFallback(date, raw, applied);
                        PriceEvent event = new PriceEvent(security.getUUID(), security.getName(), date, raw.getDate(),
                                        Double.valueOf(raw.getValue() / Values.Quote.divider()), applied.getDate(),
                                        Double.valueOf(applied.getValue() / Values.Quote.divider()), type);
                        String key = security.getUUID() + '\u0000' + date + '\u0000' + type;

                        if (!"EXACT".equals(type) && seenFallback.add(key)) //$NON-NLS-1$
                            calculation.priceFallbackEvents.add(event);

                        if (raw.getValue() == 0L && seenMissing.add(key))
                            calculation.missingPrices.add(event);
                    }
                }
                date = date.plusDays(1);
            }
        }
        catch (Exception e)
        {
            calculation.priceDiagnosticErrors.add(describe(e));
        }
    }

    private static String classifyPriceFallback(LocalDate requestedDate, SecurityPrice raw, SecurityPrice applied)
    {
        if (raw.getValue() == 0L && applied.getValue() == 0L)
            return "MISSING"; //$NON-NLS-1$
        if (raw.getValue() == 0L && applied.getValue() != 0L)
            return "LAST_TRANSACTION_GROSS_PRICE"; //$NON-NLS-1$
        if (applied.getDate().equals(requestedDate))
            return "EXACT"; //$NON-NLS-1$
        if (applied.getDate().isBefore(requestedDate))
            return "PREVIOUS_AVAILABLE_PRICE"; //$NON-NLS-1$
        return "FIRST_AVAILABLE_FUTURE_PRICE"; //$NON-NLS-1$
    }

    private static void writeReport(Path file, SourceCalculation calculation) throws IOException
    {
        JsonObject root = new JsonObject();
        root.addProperty("source_file", calculation.sourceFile()); //$NON-NLS-1$
        add(root, "reporting_currency", calculation.reportingCurrency); //$NON-NLS-1$
        add(root, "report_date", calculation.reportDate); //$NON-NLS-1$

        JsonObject periods = new JsonObject();
        periods.add("all_time", reportPeriodJson(calculation.allTime)); //$NON-NLS-1$
        periods.add("current_year", reportPeriodJson(calculation.currentYear)); //$NON-NLS-1$
        root.add("periods", periods); //$NON-NLS-1$

        add(root, "portfolio_market_value", calculation.portfolioMarketValue); //$NON-NLS-1$
        root.add("portfolio_overview", portfolioOverviewJson(calculation.portfolioOverview)); //$NON-NLS-1$

        JsonObject engine = new JsonObject();
        engine.addProperty("source", "Portfolio Performance"); //$NON-NLS-1$ //$NON-NLS-2$
        engine.addProperty("version_or_commit", ENGINE_COMMIT); //$NON-NLS-1$
        root.add("calculation_engine", engine); //$NON-NLS-1$

        Files.writeString(file, GSON.toJson(root) + System.lineSeparator(), StandardCharsets.UTF_8);
    }

    private static JsonObject portfolioOverviewJson(PortfolioOverview overview)
    {
        JsonObject json = new JsonObject();
        if (overview == null)
        {
            json.add("cash", JsonNull.INSTANCE); //$NON-NLS-1$
            json.add("scope", JsonNull.INSTANCE); //$NON-NLS-1$
            json.add("allocation_by_asset_class", new JsonArray()); //$NON-NLS-1$
            json.add("scoped_holdings", new JsonArray()); //$NON-NLS-1$
            json.add("top_holdings", new JsonArray()); //$NON-NLS-1$
            return json;
        }

        JsonObject cash = new JsonObject();
        add(cash, "market_value", overview.cash().marketValue()); //$NON-NLS-1$
        add(cash, "weight", overview.cash().weight()); //$NON-NLS-1$
        JsonArray cashAccounts = new JsonArray();
        for (CashAccountOverview item : overview.cash().accounts())
        {
            JsonObject entry = new JsonObject();
            add(entry, "account_uuid", item.accountUUID()); //$NON-NLS-1$
            add(entry, "original_currency", item.originalCurrency()); //$NON-NLS-1$
            add(entry, "original_value", item.originalValue()); //$NON-NLS-1$
            add(entry, "reporting_currency", item.reportingCurrency()); //$NON-NLS-1$
            add(entry, "reporting_value", item.reportingValue()); //$NON-NLS-1$
            cashAccounts.add(entry);
        }
        cash.add("accounts", cashAccounts); //$NON-NLS-1$
        json.add("cash", cash); //$NON-NLS-1$

        JsonObject scope = new JsonObject();
        add(scope, "mode", overview.scope().mode()); //$NON-NLS-1$
        scope.add("account_uuids", GSON.toJsonTree(overview.scope().accountUUIDs())); //$NON-NLS-1$
        scope.add("portfolio_uuids", GSON.toJsonTree(overview.scope().portfolioUUIDs())); //$NON-NLS-1$
        json.add("scope", scope); //$NON-NLS-1$

        JsonArray allocation = new JsonArray();
        for (AllocationOverview item : overview.allocationByAssetClass())
        {
            JsonObject entry = new JsonObject();
            add(entry, "name", item.name()); //$NON-NLS-1$
            add(entry, "market_value", item.marketValue()); //$NON-NLS-1$
            add(entry, "weight", item.weight()); //$NON-NLS-1$
            allocation.add(entry);
        }
        json.add("allocation_by_asset_class", allocation); //$NON-NLS-1$

        JsonArray scopedHoldings = holdingOverviewJson(overview.scopedHoldings());
        json.add("scoped_holdings", scopedHoldings); //$NON-NLS-1$
        json.add("top_holdings", holdingOverviewJson(overview.topHoldings())); //$NON-NLS-1$
        return json;
    }

    private static JsonArray holdingOverviewJson(List<HoldingOverview> items)
    {
        JsonArray holdings = new JsonArray();
        for (HoldingOverview item : items)
        {
            JsonObject entry = new JsonObject();
            add(entry, "security_uuid", item.securityUUID()); //$NON-NLS-1$
            add(entry, "name", item.name()); //$NON-NLS-1$
            add(entry, "market_value", item.marketValue()); //$NON-NLS-1$
            add(entry, "weight", item.weight()); //$NON-NLS-1$
            add(entry, "currency", item.currency()); //$NON-NLS-1$
            add(entry, "asset_class", item.assetClass()); //$NON-NLS-1$
            holdings.add(entry);
        }
        return holdings;
    }

    private static JsonObject reportPeriodJson(PeriodResult result)
    {
        JsonObject json = new JsonObject();
        add(json, "period_code", result.periodCode); //$NON-NLS-1$
        add(json, "requested_start_date", start(result.requestedInterval)); //$NON-NLS-1$
        add(json, "requested_end_date", end(result.requestedInterval)); //$NON-NLS-1$
        add(json, "effective_start_date", start(result.effectiveInterval)); //$NON-NLS-1$
        add(json, "effective_end_date", end(result.effectiveInterval)); //$NON-NLS-1$
        add(json, "cumulative_ttwror", result.cumulativeTtwror); //$NON-NLS-1$
        add(json, "annualized_ttwror", result.annualizedTtwror); //$NON-NLS-1$
        add(json, "irr", result.irr); //$NON-NLS-1$
        add(json, "profit", result.profit); //$NON-NLS-1$
        return json;
    }

    private static void writeSeries(Path file, PeriodResult period) throws IOException
    {
        try (BufferedWriter writer = Files.newBufferedWriter(file, StandardCharsets.UTF_8))
        {
            writer.write("date,portfolio_market_value,period_profit,cumulative_ttwror,daily_ttwror"); //$NON-NLS-1$
            writer.newLine();

            if (period.index == null)
                return;

            LocalDate[] dates = period.index.getDates();
            long[] totals = period.index.getTotals();
            long[] profits = period.index.calculateDelta();
            double[] cumulative = period.index.getAccumulatedPercentage();
            double[] daily = period.index.getDeltaPercentage();

            for (int ii = 0; ii < dates.length; ii++)
            {
                writer.write(dates[ii].toString());
                writer.write(',');
                writer.write(decimal(amount(totals[ii])));
                writer.write(',');
                writer.write(decimal(amount(profits[ii])));
                writer.write(',');
                writer.write(decimal(finite(cumulative[ii])));
                writer.write(',');
                writer.write(decimal(finite(daily[ii])));
                writer.newLine();
            }
        }
    }

    private static void writeDiagnostics(Path file, SourceCalculation calculation) throws IOException
    {
        JsonObject root = new JsonObject();
        root.addProperty("source_file", calculation.sourceFile()); //$NON-NLS-1$
        add(root, "source_xml_sha256_before", calculation.sha256Before); //$NON-NLS-1$
        add(root, "source_xml_sha256_after", calculation.sha256After); //$NON-NLS-1$
        root.addProperty("source_xml_unchanged", calculation.sha256Before != null //$NON-NLS-1$
                        && calculation.sha256Before.equals(calculation.sha256After));
        add(root, "model_version", calculation.modelVersion); //$NON-NLS-1$

        JsonObject engine = new JsonObject();
        engine.addProperty("commit", ENGINE_COMMIT); //$NON-NLS-1$
        engine.addProperty("project_version", ENGINE_PROJECT_VERSION); //$NON-NLS-1$
        root.add("portfolio_performance", engine); //$NON-NLS-1$

        add(root, "reporting_currency", calculation.reportingCurrency); //$NON-NLS-1$
        add(root, "report_date", calculation.reportDate); //$NON-NLS-1$
        root.add("entity_counts", calculation.stats != null ? calculation.stats.toJson() : JsonNull.INSTANCE); //$NON-NLS-1$

        JsonObject intervals = new JsonObject();
        intervals.add("all_time", intervalJson(calculation.allTime)); //$NON-NLS-1$
        intervals.add("current_year", intervalJson(calculation.currentYear)); //$NON-NLS-1$
        root.add("requested_and_effective_intervals", intervals); //$NON-NLS-1$

        JsonObject dataSeries = new JsonObject();
        dataSeries.add("all_time", filterJson(calculation.allTime.filter)); //$NON-NLS-1$
        dataSeries.add("current_year", filterJson(calculation.currentYear.filter)); //$NON-NLS-1$
        root.add("resolved_data_series", dataSeries); //$NON-NLS-1$

        JsonObject periods = new JsonObject();
        periods.add("all_time", periodDiagnosticJson(calculation.allTime)); //$NON-NLS-1$
        periods.add("current_year", periodDiagnosticJson(calculation.currentYear)); //$NON-NLS-1$
        root.add("periods", periods); //$NON-NLS-1$

        root.add("fx", calculation.fx.toJson()); //$NON-NLS-1$

        JsonObject prices = new JsonObject();
        prices.addProperty("missing_price_count", calculation.missingPrices.size()); //$NON-NLS-1$
        prices.add("missing_prices", GSON.toJsonTree(calculation.missingPrices)); //$NON-NLS-1$
        prices.addProperty("price_fallback_event_count", calculation.priceFallbackEvents.size()); //$NON-NLS-1$
        prices.add("price_fallback_events", GSON.toJsonTree(calculation.priceFallbackEvents)); //$NON-NLS-1$
        prices.add("diagnostic_errors", GSON.toJsonTree(calculation.priceDiagnosticErrors)); //$NON-NLS-1$
        root.add("prices", prices); //$NON-NLS-1$

        root.add("current_year_boundary", boundaryJson(calculation)); //$NON-NLS-1$
        root.add("engine_warnings", GSON.toJsonTree(calculation.engineWarnings)); //$NON-NLS-1$
        root.add("period_errors", GSON.toJsonTree(calculation.periodErrors)); //$NON-NLS-1$
        root.add("post_calculation_errors", GSON.toJsonTree(calculation.postCalculationErrors)); //$NON-NLS-1$
        add(root, "fatal_error", calculation.fatalError); //$NON-NLS-1$

        Files.writeString(file, GSON.toJson(root) + System.lineSeparator(), StandardCharsets.UTF_8);
    }

    private static JsonObject intervalJson(PeriodResult result)
    {
        JsonObject json = new JsonObject();
        add(json, "period_code", result.periodCode); //$NON-NLS-1$
        add(json, "requested_start_date", start(result.requestedInterval)); //$NON-NLS-1$
        add(json, "requested_end_date", end(result.requestedInterval)); //$NON-NLS-1$
        add(json, "effective_start_date", start(result.effectiveInterval)); //$NON-NLS-1$
        add(json, "effective_end_date", end(result.effectiveInterval)); //$NON-NLS-1$
        return json;
    }

    private static JsonObject periodDiagnosticJson(PeriodResult result)
    {
        JsonObject json = intervalJson(result);
        add(json, "start_valuation", result.startValuation); //$NON-NLS-1$
        add(json, "end_valuation", result.endValuation); //$NON-NLS-1$
        add(json, "ending_total_market_value", result.endingTotalMarketValue); //$NON-NLS-1$
        add(json, "absolute_change", result.absoluteChange); //$NON-NLS-1$
        add(json, "period_profit_delta", result.profit); //$NON-NLS-1$
        add(json, "cumulative_ttwror_start", result.startCumulativeTtwror); //$NON-NLS-1$
        add(json, "cumulative_ttwror_end", result.endCumulativeTtwror); //$NON-NLS-1$
        json.add("warnings", GSON.toJsonTree(result.warnings)); //$NON-NLS-1$
        add(json, "error", result.error); //$NON-NLS-1$
        return json;
    }

    private static JsonObject filterJson(ResolvedFilter filter)
    {
        if (filter == null)
            return nullJsonObject();

        JsonObject json = new JsonObject();
        json.addProperty("type", filter.type()); //$NON-NLS-1$
        json.addProperty("filter_uuid", filter.uuid()); //$NON-NLS-1$
        json.addProperty("filter_name", filter.name()); //$NON-NLS-1$
        json.add("components", GSON.toJsonTree(filter.components())); //$NON-NLS-1$
        json.add("account_closure_uuids", GSON.toJsonTree(filter.accountClosureUUIDs())); //$NON-NLS-1$
        json.add("portfolio_closure_uuids", GSON.toJsonTree(filter.portfolioClosureUUIDs())); //$NON-NLS-1$
        return json;
    }

    private static JsonObject nullJsonObject()
    {
        JsonObject json = new JsonObject();
        json.add("type", JsonNull.INSTANCE); //$NON-NLS-1$
        json.add("filter_uuid", JsonNull.INSTANCE); //$NON-NLS-1$
        json.add("filter_name", JsonNull.INSTANCE); //$NON-NLS-1$
        json.add("components", new JsonArray()); //$NON-NLS-1$
        json.add("account_closure_uuids", new JsonArray()); //$NON-NLS-1$
        json.add("portfolio_closure_uuids", new JsonArray()); //$NON-NLS-1$
        return json;
    }

    private static JsonObject boundaryJson(SourceCalculation calculation)
    {
        if (calculation.boundary == null)
        {
            JsonObject json = new JsonObject();
            add(json, "error", calculation.boundaryError); //$NON-NLS-1$
            return json;
        }

        BoundaryDiagnostics boundary = calculation.boundary;
        JsonObject json = new JsonObject();
        add(json, "date", boundary.date); //$NON-NLS-1$
        add(json, "total_portfolio_valuation", boundary.totalPortfolioValuation); //$NON-NLS-1$
        add(json, "snapshot_total", boundary.snapshotTotal); //$NON-NLS-1$
        add(json, "all_time_cumulative_ttwror_at_start", boundary.allTimeCumulativeTtwrorAtStart); //$NON-NLS-1$
        add(json, "all_time_cumulative_ttwror_at_end", boundary.allTimeCumulativeTtwrorAtEnd); //$NON-NLS-1$
        add(json, "all_time_profit_at_start", boundary.allTimeProfitAtStart); //$NON-NLS-1$
        add(json, "all_time_profit_at_end", boundary.allTimeProfitAtEnd); //$NON-NLS-1$
        json.add("account_cash_balances", GSON.toJsonTree(boundary.accounts)); //$NON-NLS-1$
        json.add("portfolio_security_market_values", GSON.toJsonTree(boundary.portfolios)); //$NON-NLS-1$
        json.add("applied_security_prices", GSON.toJsonTree(boundary.appliedSecurityPrices)); //$NON-NLS-1$
        json.add("applied_fx_rates", calculation.fx.toJson().get("currency_pairs").deepCopy()); //$NON-NLS-1$ //$NON-NLS-2$

        JsonObject transactions = new JsonObject();
        boundary.transactions.forEach((date, values) -> transactions.add(date.toString(), GSON.toJsonTree(values)));
        json.add("transactions_near_boundary", transactions); //$NON-NLS-1$

        JsonObject filter = new JsonObject();
        add(filter, "data_series_type", boundary.dataSeriesType); //$NON-NLS-1$
        add(filter, "filter_uuid", boundary.filterUUID); //$NON-NLS-1$
        filter.add("components", GSON.toJsonTree(boundary.filterComponents)); //$NON-NLS-1$
        json.add("resolved_data_series", filter); //$NON-NLS-1$

        JsonObject ttwror = new JsonObject();
        ttwror.addProperty("formula", //$NON-NLS-1$
                        "(1 + all_time_ttwror_at_end) / (1 + all_time_ttwror_at_start) - 1"); //$NON-NLS-1$
        add(ttwror, "value_from_boundary_identity", boundary.ttwrorFromBoundaryIdentity); //$NON-NLS-1$
        add(ttwror, "value_from_current_year_index", boundary.ttwrorFromCurrentYearIndex); //$NON-NLS-1$
        add(ttwror, "difference", boundary.ttwrorIdentityDifference); //$NON-NLS-1$
        json.add("ttwror_identity", ttwror); //$NON-NLS-1$

        JsonObject profit = new JsonObject();
        profit.addProperty("formula", "all_time_profit_at_end - all_time_profit_at_start"); //$NON-NLS-1$ //$NON-NLS-2$
        add(profit, "value_from_boundary_identity", boundary.profitFromBoundaryIdentity); //$NON-NLS-1$
        add(profit, "value_from_current_year_index", boundary.profitFromCurrentYearIndex); //$NON-NLS-1$
        add(profit, "difference", boundary.profitIdentityDifference); //$NON-NLS-1$
        json.add("profit_identity", profit); //$NON-NLS-1$

        return json;
    }

    private static void writeComparison(Path file, SourceCalculation main, SourceCalculation backup) throws IOException
    {
        StringBuilder markdown = new StringBuilder();
        markdown.append("# XML version comparison\n\n"); //$NON-NLS-1$
        markdown.append("Backup используется только для диагностики и не считается эталоном.\n\n"); //$NON-NLS-1$

        if (backup == null)
        {
            markdown.append("Comparison XML не найден рядом с основным файлом и не был передан третьим аргументом.\n"); //$NON-NLS-1$
            Files.writeString(file, markdown.toString(), StandardCharsets.UTF_8);
            return;
        }

        markdown.append("| Параметр | Основной XML | Backup XML | Различие |\n"); //$NON-NLS-1$
        markdown.append("|---|---:|---:|---:|\n"); //$NON-NLS-1$
        comparisonRow(markdown, "SHA-256", main.sha256Before, backup.sha256Before, //$NON-NLS-1$
                        Objects.equals(main.sha256Before, backup.sha256Before) ? "нет" : "да"); //$NON-NLS-1$ //$NON-NLS-2$
        comparisonRow(markdown, "Report date", text(main.reportDate), text(backup.reportDate), //$NON-NLS-1$
                        differenceText(main.reportDate, backup.reportDate));
        comparisonRow(markdown, "Securities", stat(main, EntityStats::securities), //$NON-NLS-1$
                        stat(backup, EntityStats::securities), integerDifference(main, backup, EntityStats::securities));
        comparisonRow(markdown, "Accounts", stat(main, EntityStats::accounts), stat(backup, EntityStats::accounts), //$NON-NLS-1$
                        integerDifference(main, backup, EntityStats::accounts));
        comparisonRow(markdown, "Portfolios", stat(main, EntityStats::portfolios), //$NON-NLS-1$
                        stat(backup, EntityStats::portfolios), integerDifference(main, backup, EntityStats::portfolios));
        comparisonRow(markdown, "Transactions", stat(main, EntityStats::transactions), //$NON-NLS-1$
                        stat(backup, EntityStats::transactions),
                        integerDifference(main, backup, EntityStats::transactions));
        comparisonRow(markdown, "Current-year boundary valuation", //$NON-NLS-1$
                        boundaryValue(main, value -> value.totalPortfolioValuation),
                        boundaryValue(backup, value -> value.totalPortfolioValuation),
                        doubleDifference(boundaryDouble(main, value -> value.totalPortfolioValuation),
                                        boundaryDouble(backup, value -> value.totalPortfolioValuation)));
        comparisonRow(markdown, "Current-year cumulative TTWROR", decimal(main.currentYear.cumulativeTtwror), //$NON-NLS-1$
                        decimal(backup.currentYear.cumulativeTtwror),
                        doubleDifference(main.currentYear.cumulativeTtwror, backup.currentYear.cumulativeTtwror));
        comparisonRow(markdown, "Current-year Profit", decimal(main.currentYear.profit), //$NON-NLS-1$
                        decimal(backup.currentYear.profit),
                        doubleDifference(main.currentYear.profit, backup.currentYear.profit));
        comparisonRow(markdown, "Ending market value", decimal(main.portfolioMarketValue), //$NON-NLS-1$
                        decimal(backup.portfolioMarketValue),
                        doubleDifference(main.portfolioMarketValue, backup.portfolioMarketValue));

        Map<String, String> mainTransactions = transactionSignatures(main.client);
        Map<String, String> backupTransactions = transactionSignatures(backup.client);
        List<String> transactionDifferences = mapDifferences(mainTransactions, backupTransactions);
        Map<String, String> mainPrices = priceSignatures(main.client);
        Map<String, String> backupPrices = priceSignatures(backup.client);
        List<String> priceDifferences = mapDifferences(mainPrices, backupPrices);

        markdown.append("\n## Financial data differences\n\n"); //$NON-NLS-1$
        markdown.append("- Различающихся transactions: **").append(transactionDifferences.size()).append("**.\n"); //$NON-NLS-1$ //$NON-NLS-2$
        markdown.append("- Различающихся historical/latest prices: **").append(priceDifferences.size()) //$NON-NLS-1$
                        .append("**.\n"); //$NON-NLS-1$

        appendDifferences(markdown, "Transactions", transactionDifferences); //$NON-NLS-1$
        appendDifferences(markdown, "Prices", priceDifferences); //$NON-NLS-1$

        if (!Objects.equals(main.sha256Before, backup.sha256Before) && transactionDifferences.isEmpty()
                        && priceDifferences.isEmpty())
        {
            markdown.append("\n**Установленный факт:** SHA-256 файлов различается, но финансовые transactions и prices ") //$NON-NLS-1$
                            .append("совпадают. Поэтому различие файлов находится вне сравниваемого финансового состояния ") //$NON-NLS-1$
                            .append("(например, в dashboard/view configuration) и не объясняет расхождение метрик.\n"); //$NON-NLS-1$
        }

        Files.writeString(file, markdown.toString(), StandardCharsets.UTF_8);
    }

    private static String writeValidation(Path file, SourceCalculation calculation) throws IOException
    {
        String overallStatus = overallStatus(calculation);
        StringBuilder markdown = new StringBuilder();
        markdown.append("# Validation report\n\n"); //$NON-NLS-1$
        markdown.append("- Overall status: `").append(overallStatus).append("`\n"); //$NON-NLS-1$ //$NON-NLS-2$
        markdown.append("- Source XML: `").append(escapeMarkdown(calculation.sourceFile())).append("`\n"); //$NON-NLS-1$ //$NON-NLS-2$
        markdown.append("- Reporting currency: `") //$NON-NLS-1$
                        .append(calculation.reportingCurrency != null ? calculation.reportingCurrency : "null") //$NON-NLS-1$
                        .append("`\n"); //$NON-NLS-1$
        markdown.append("- Engine: Portfolio Performance commit `").append(ENGINE_COMMIT).append("`\n\n"); //$NON-NLS-1$ //$NON-NLS-2$

        markdown.append("## Metrics calculated by Portfolio Performance\n\n"); //$NON-NLS-1$
        markdown.append("| Показатель | Рассчитанное значение | Статус |\n"); //$NON-NLS-1$
        markdown.append("|---|---:|---|\n"); //$NON-NLS-1$
        appendCalculatedMetric(markdown, "all_time.cumulative_ttwror", calculation.allTime.cumulativeTtwror, //$NON-NLS-1$
                        "%", true, calculation.allTime.error); //$NON-NLS-1$
        appendCalculatedMetric(markdown, "all_time.annualized_ttwror", calculation.allTime.annualizedTtwror, //$NON-NLS-1$
                        "%", true, calculation.allTime.error); //$NON-NLS-1$
        appendCalculatedMetric(markdown, "all_time.irr", calculation.allTime.irr, "%", true, //$NON-NLS-1$ //$NON-NLS-2$
                        calculation.allTime.error);
        appendCalculatedMetric(markdown, "all_time.profit", calculation.allTime.profit, //$NON-NLS-1$
                        calculation.reportingCurrency, false, calculation.allTime.error);
        appendCalculatedMetric(markdown, "current_year.cumulative_ttwror", //$NON-NLS-1$
                        calculation.currentYear.cumulativeTtwror, "%", true, calculation.currentYear.error); //$NON-NLS-1$
        appendCalculatedMetric(markdown, "current_year.annualized_ttwror", //$NON-NLS-1$
                        calculation.currentYear.annualizedTtwror, "%", true, calculation.currentYear.error); //$NON-NLS-1$
        appendCalculatedMetric(markdown, "current_year.irr", calculation.currentYear.irr, "%", true, //$NON-NLS-1$ //$NON-NLS-2$
                        calculation.currentYear.error);
        appendCalculatedMetric(markdown, "current_year.profit", calculation.currentYear.profit, //$NON-NLS-1$
                        calculation.reportingCurrency, false, calculation.currentYear.error);
        appendCalculatedMetric(markdown, "portfolio_market_value", calculation.portfolioMarketValue, //$NON-NLS-1$
                        calculation.reportingCurrency, false, calculation.allTime.error);

        markdown.append("\n## Effective periods\n\n"); //$NON-NLS-1$
        appendPeriodSummary(markdown, "all_time", calculation.allTime); //$NON-NLS-1$
        appendPeriodSummary(markdown, "current_year", calculation.currentYear); //$NON-NLS-1$
        markdown.append("\nМетрики относятся к effective interval. Будущая часть сохранённого периода после ") //$NON-NLS-1$
                        .append("`report_date` не рассчитывается. Все финансовые значения получены из официального ") //$NON-NLS-1$
                        .append("`PerformanceIndex` и snapshot-классов Portfolio Performance.\n"); //$NON-NLS-1$

        if (calculation.boundary != null)
        {
            markdown.append("\n## Current-year identity checks\n\n"); //$NON-NLS-1$
            markdown.append("- TTWROR через all-time boundaries: `") //$NON-NLS-1$
                            .append(decimal(calculation.boundary.ttwrorFromBoundaryIdentity)).append("`; отдельный index: `") //$NON-NLS-1$
                            .append(decimal(calculation.boundary.ttwrorFromCurrentYearIndex)).append("`; difference: `") //$NON-NLS-1$
                            .append(decimal(calculation.boundary.ttwrorIdentityDifference)).append("`.\n"); //$NON-NLS-1$
            markdown.append("- Profit через all-time boundaries: `") //$NON-NLS-1$
                            .append(decimal(calculation.boundary.profitFromBoundaryIdentity)).append("`; отдельный index: `") //$NON-NLS-1$
                            .append(decimal(calculation.boundary.profitFromCurrentYearIndex)).append("`; difference: `") //$NON-NLS-1$
                            .append(decimal(calculation.boundary.profitIdentityDifference)).append("`.\n"); //$NON-NLS-1$
        }

        if (calculation.fatalError != null || !calculation.periodErrors.isEmpty()
                        || !calculation.engineWarnings.isEmpty() || calculation.fx.hasFatalFallback())
        {
            markdown.append("\n## Диагностика ошибок\n\n"); //$NON-NLS-1$
            if (calculation.fatalError != null)
                markdown.append("- Fatal: `").append(escapeMarkdown(calculation.fatalError)).append("`\n"); //$NON-NLS-1$ //$NON-NLS-2$
            calculation.periodErrors.forEach(error -> markdown.append("- Period error: `") //$NON-NLS-1$
                            .append(escapeMarkdown(error)).append("`\n")); //$NON-NLS-1$
            calculation.engineWarnings.forEach(warning -> markdown.append("- Engine warning: `") //$NON-NLS-1$
                            .append(escapeMarkdown(warning)).append("`\n")); //$NON-NLS-1$
            if (calculation.fx.hasFatalFallback())
                markdown.append("- Missing FX/fallback rate `1` detected; результат не считается проверенным.\n"); //$NON-NLS-1$
        }

        Files.writeString(file, markdown.toString(), StandardCharsets.UTF_8);
        return overallStatus;
    }

    private static void appendCalculatedMetric(StringBuilder markdown, String name, Double value, String unit,
                    boolean ratio, String error)
    {
        markdown.append("| `").append(name).append("` | "); //$NON-NLS-1$ //$NON-NLS-2$
        if (value != null)
        {
            double displayed = ratio ? value.doubleValue() * 100d : value.doubleValue();
            markdown.append(String.format(Locale.ROOT, "%.9f %s", displayed, unit != null ? unit : "")) //$NON-NLS-1$ //$NON-NLS-2$
                            .append(" | `CALCULATED` |\n"); //$NON-NLS-1$
        }
        else
        {
            markdown.append("null | `NOT_CALCULATED`") //$NON-NLS-1$
                            .append(error != null ? ": " + escapeMarkdown(error) : "") //$NON-NLS-1$ //$NON-NLS-2$
                            .append(" |\n"); //$NON-NLS-1$
        }
    }

    private static void appendPeriodSummary(StringBuilder markdown, String name, PeriodResult period)
    {
        markdown.append("- `").append(name).append("`: code `").append(period.periodCode).append("`, requested `") //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$
                        .append(intervalText(period.requestedInterval)).append("`, effective `") //$NON-NLS-1$ //$NON-NLS-2$
                        .append(intervalText(period.effectiveInterval)).append("`.\n"); //$NON-NLS-1$
    }

    private static String overallStatus(SourceCalculation calculation)
    {
        if (calculation.fx.hasFatalFallback() || calculation.fatalError != null)
            return "NOT_YET_CALCULABLE"; //$NON-NLS-1$

        Double[] metrics = { calculation.allTime.cumulativeTtwror, calculation.allTime.annualizedTtwror,
                        calculation.allTime.irr, calculation.allTime.profit, calculation.currentYear.cumulativeTtwror,
                        calculation.currentYear.annualizedTtwror, calculation.currentYear.irr,
                        calculation.currentYear.profit, calculation.portfolioMarketValue };
        long calculated = java.util.Arrays.stream(metrics).filter(Objects::nonNull).count();

        if (calculated == 0)
            return "NOT_YET_CALCULABLE"; //$NON-NLS-1$
        return calculated == metrics.length ? "CALCULATED" : "PARTIALLY_CALCULATED"; //$NON-NLS-1$ //$NON-NLS-2$
    }

    private static void comparisonRow(StringBuilder markdown, String label, String main, String backup,
                    String difference)
    {
        markdown.append("| ").append(label).append(" | ").append(escapeMarkdown(main)).append(" | ") //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$
                        .append(escapeMarkdown(backup)).append(" | ").append(escapeMarkdown(difference)).append(" |\n"); //$NON-NLS-1$ //$NON-NLS-2$
    }

    private static String stat(SourceCalculation calculation,
                    java.util.function.ToIntFunction<EntityStats> getter)
    {
        return calculation.stats != null ? Integer.toString(getter.applyAsInt(calculation.stats)) : "null"; //$NON-NLS-1$
    }

    private static String integerDifference(SourceCalculation left, SourceCalculation right,
                    java.util.function.ToIntFunction<EntityStats> getter)
    {
        if (left.stats == null || right.stats == null)
            return "null"; //$NON-NLS-1$
        return Integer.toString(getter.applyAsInt(left.stats) - getter.applyAsInt(right.stats));
    }

    private static String differenceText(Object left, Object right)
    {
        return Objects.equals(left, right) ? "нет" : "да"; //$NON-NLS-1$ //$NON-NLS-2$
    }

    private static String boundaryValue(SourceCalculation calculation,
                    java.util.function.Function<BoundaryDiagnostics, Double> getter)
    {
        return decimal(boundaryDouble(calculation, getter));
    }

    private static Double boundaryDouble(SourceCalculation calculation,
                    java.util.function.Function<BoundaryDiagnostics, Double> getter)
    {
        return calculation.boundary != null ? getter.apply(calculation.boundary) : null;
    }

    private static String doubleDifference(Double left, Double right)
    {
        return decimal(difference(left, right));
    }

    private static Double difference(Double left, Double right)
    {
        return left != null && right != null ? Double.valueOf(left.doubleValue() - right.doubleValue()) : null;
    }

    private static Map<String, String> transactionSignatures(Client client)
    {
        Map<String, String> answer = new TreeMap<>();
        if (client == null)
            return answer;

        for (Account account : client.getAccounts())
        {
            for (AccountTransaction transaction : account.getTransactions())
                answer.put(transaction.getUUID(), signature(account.getUUID(), transaction, transaction.getType().name()));
        }
        for (Portfolio portfolio : client.getPortfolios())
        {
            for (PortfolioTransaction transaction : portfolio.getTransactions())
                answer.put(transaction.getUUID(),
                                signature(portfolio.getUUID(), transaction, transaction.getType().name()));
        }
        return answer;
    }

    private static String signature(String ownerUUID, Transaction transaction, String type)
    {
        String units = transaction.getUnits().map(unit -> unit.getType() + ":" + unit.getAmount() + ":" //$NON-NLS-1$ //$NON-NLS-2$
                        + unit.getForex()).collect(Collectors.joining(",")); //$NON-NLS-1$
        Security security = transaction.getSecurity();
        return ownerUUID + "|" + transaction.getDateTime() + "|" + type + "|" + transaction.getCurrencyCode() + "|" //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$ //$NON-NLS-4$
                        + transaction.getAmount() + "|" + transaction.getShares() + "|" //$NON-NLS-1$ //$NON-NLS-2$
                        + (security != null ? security.getUUID() : "") + "|" + units; //$NON-NLS-1$ //$NON-NLS-2$
    }

    private static Map<String, String> priceSignatures(Client client)
    {
        Map<String, String> answer = new TreeMap<>();
        if (client == null)
            return answer;

        for (Security security : client.getSecurities())
        {
            for (SecurityPrice price : security.getPrices())
                answer.put(security.getUUID() + "|H|" + price.getDate(), Long.toString(price.getValue())); //$NON-NLS-1$
            LatestSecurityPrice latest = security.getLatest();
            if (latest != null)
                answer.put(security.getUUID() + "|L|" + latest.getDate(), //$NON-NLS-1$
                                latest.getValue() + "|" + latest.getHigh() + "|" + latest.getLow() + "|" //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$
                                                + latest.getVolume());
        }
        return answer;
    }

    private static List<String> mapDifferences(Map<String, String> left, Map<String, String> right)
    {
        Set<String> keys = new TreeSet<>();
        keys.addAll(left.keySet());
        keys.addAll(right.keySet());
        return keys.stream().filter(key -> !Objects.equals(left.get(key), right.get(key)))
                        .map(key -> key + ": main=" + left.get(key) + ", backup=" + right.get(key)).toList(); //$NON-NLS-1$ //$NON-NLS-2$
    }

    private static void appendDifferences(StringBuilder markdown, String title, List<String> differences)
    {
        markdown.append("\n### ").append(title).append("\n\n"); //$NON-NLS-1$ //$NON-NLS-2$
        if (differences.isEmpty())
        {
            markdown.append("Различий не обнаружено.\n"); //$NON-NLS-1$
        }
        else
        {
            differences.forEach(value -> markdown.append("- `").append(escapeMarkdown(value)).append("`\n")); //$NON-NLS-1$ //$NON-NLS-2$
        }
    }

    private static void add(JsonObject object, String key, String value)
    {
        if (value == null)
            object.add(key, JsonNull.INSTANCE);
        else
            object.addProperty(key, value);
    }

    private static void add(JsonObject object, String key, Integer value)
    {
        if (value == null)
            object.add(key, JsonNull.INSTANCE);
        else
            object.addProperty(key, value);
    }

    private static void add(JsonObject object, String key, LocalDate value)
    {
        add(object, key, value != null ? value.toString() : null);
    }

    private static void add(JsonObject object, String key, Double value)
    {
        if (value == null)
            object.add(key, JsonNull.INSTANCE);
        else
            object.addProperty(key, value);
    }

    private static LocalDate start(Interval interval)
    {
        return interval != null ? interval.getStart() : null;
    }

    private static LocalDate end(Interval interval)
    {
        return interval != null ? interval.getEnd() : null;
    }

    private static Double amount(long minorUnits)
    {
        return Double.valueOf(minorUnits / Values.Amount.divider());
    }

    private static Double finite(double value)
    {
        return Double.isFinite(value) ? Double.valueOf(value) : null;
    }

    private static String decimal(Double value)
    {
        if (value == null)
            return "null"; //$NON-NLS-1$
        return BigDecimal.valueOf(value.doubleValue()).stripTrailingZeros().toPlainString();
    }

    private static String text(Object value)
    {
        return value != null ? value.toString() : "null"; //$NON-NLS-1$
    }

    private static String intervalText(Interval interval)
    {
        return interval != null ? "(" + interval.getStart() + ", " + interval.getEnd() + "]" : "null"; //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$ //$NON-NLS-4$
    }

    private static String describe(Throwable throwable)
    {
        String message = throwable.getMessage();
        return throwable.getClass().getSimpleName() + (message != null && !message.isBlank() ? ": " + message : ""); //$NON-NLS-1$ //$NON-NLS-2$
    }

    private static String escapeMarkdown(String value)
    {
        return value == null ? "" : value.replace("|", "\\|").replace("\r", " ").replace("\n", " "); //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$ //$NON-NLS-4$ //$NON-NLS-5$
    }

    private static String sha256(Path path) throws IOException
    {
        try
        {
            MessageDigest digest = MessageDigest.getInstance("SHA-256"); //$NON-NLS-1$
            try (InputStream stream = Files.newInputStream(path))
            {
                byte[] buffer = new byte[65536];
                int read;
                while ((read = stream.read(buffer)) >= 0)
                    digest.update(buffer, 0, read);
            }

            StringBuilder answer = new StringBuilder();
            for (byte value : digest.digest())
                answer.append(String.format(Locale.ROOT, "%02x", Byte.valueOf(value))); //$NON-NLS-1$
            return answer.toString();
        }
        catch (NoSuchAlgorithmException e)
        {
            throw new IllegalStateException(e);
        }
    }

    private record CashAccountOverview(String accountUUID, String originalCurrency, Double originalValue,
                    String reportingCurrency, Double reportingValue, long reportingMinorUnits)
    {
    }

    private record CashOverview(Double marketValue, Double weight, List<CashAccountOverview> accounts)
    {
    }

    private record AllocationOverview(String name, Double marketValue, Double weight)
    {
    }

    private record HoldingOverview(String securityUUID, String name, Double marketValue, Double weight,
                    String currency, String assetClass)
    {
    }

    private record ScopeOverview(String mode, List<String> accountUUIDs, List<String> portfolioUUIDs)
    {
    }

    private record PortfolioOverview(CashOverview cash, ScopeOverview scope,
                    List<AllocationOverview> allocationByAssetClass, List<HoldingOverview> scopedHoldings,
                    List<HoldingOverview> topHoldings)
    {
    }

    private static final class SourceCalculation
    {
        private final Path input;
        private String sha256Before;
        private String sha256After;
        private Client client;
        private Integer modelVersion;
        private String reportingCurrency;
        private LocalDate reportDate;
        private EntityStats stats;
        private PeriodResult allTime = new PeriodResult();
        private PeriodResult currentYear = new PeriodResult();
        private Double portfolioMarketValue;
        private PortfolioOverview portfolioOverview;
        private FxDiagnostics fx = new FxDiagnostics();
        private BoundaryDiagnostics boundary;
        private String boundaryError;
        private final List<PriceEvent> missingPrices = new ArrayList<>();
        private final List<PriceEvent> priceFallbackEvents = new ArrayList<>();
        private final List<String> priceDiagnosticErrors = new ArrayList<>();
        private final List<String> engineWarnings = new ArrayList<>();
        private final List<String> periodErrors = new ArrayList<>();
        private final List<String> postCalculationErrors = new ArrayList<>();
        private String fatalError;

        private SourceCalculation(Path input)
        {
            this.input = input;
        }

        private String sourceFile()
        {
            return input.getFileName() != null ? input.getFileName().toString() : input.toString();
        }

        private boolean hasAnyCalculatedMetric()
        {
            return allTime.cumulativeTtwror != null || allTime.annualizedTtwror != null || allTime.profit != null
                            || currentYear.cumulativeTtwror != null || currentYear.annualizedTtwror != null
                            || currentYear.profit != null || portfolioMarketValue != null;
        }
    }

    private static final class PeriodResult
    {
        private String periodCode;
        private Interval requestedInterval;
        private Interval effectiveInterval;
        private ResolvedFilter filter;
        private PerformanceIndex index;
        private Double cumulativeTtwror;
        private Double annualizedTtwror;
        private Double irr;
        private Double profit;
        private Double startValuation;
        private Double endValuation;
        private Double endingTotalMarketValue;
        private Double absoluteChange;
        private Double startCumulativeTtwror;
        private Double endCumulativeTtwror;
        private List<String> warnings = List.of();
        private String error;
    }

    private static final class BoundaryDiagnostics
    {
        private LocalDate date;
        private Double totalPortfolioValuation;
        private Double snapshotTotal;
        private Double allTimeCumulativeTtwrorAtStart;
        private Double allTimeCumulativeTtwrorAtEnd;
        private Double allTimeProfitAtStart;
        private Double allTimeProfitAtEnd;
        private List<AccountDiagnostic> accounts = List.of();
        private List<PortfolioDiagnostic> portfolios = List.of();
        private List<AppliedPriceDiagnostic> appliedSecurityPrices = List.of();
        private Map<LocalDate, List<TransactionDiagnostic>> transactions = Map.of();
        private String dataSeriesType;
        private String filterUUID;
        private List<FilterComponent> filterComponents = List.of();
        private Double ttwrorFromBoundaryIdentity;
        private Double ttwrorFromCurrentYearIndex;
        private Double ttwrorIdentityDifference;
        private Double profitFromBoundaryIdentity;
        private Double profitFromCurrentYearIndex;
        private Double profitIdentityDifference;
    }

    private record EntityStats(int securities, int accounts, int portfolios, int accountTransactions,
                    int portfolioTransactions, int transactions, int historicalPrices)
    {
        static EntityStats from(Client client)
        {
            int accountTransactions = client.getAccounts().stream().mapToInt(account -> account.getTransactions().size())
                            .sum();
            int portfolioTransactions = client.getPortfolios().stream()
                            .mapToInt(portfolio -> portfolio.getTransactions().size()).sum();
            int historicalPrices = client.getSecurities().stream().mapToInt(security -> security.getPrices().size())
                            .sum();
            return new EntityStats(client.getSecurities().size(), client.getAccounts().size(),
                            client.getPortfolios().size(), accountTransactions, portfolioTransactions,
                            accountTransactions + portfolioTransactions, historicalPrices);
        }

        JsonObject toJson()
        {
            JsonObject json = new JsonObject();
            json.addProperty("securities", securities); //$NON-NLS-1$
            json.addProperty("accounts", accounts); //$NON-NLS-1$
            json.addProperty("portfolios", portfolios); //$NON-NLS-1$
            json.addProperty("account_transactions", accountTransactions); //$NON-NLS-1$
            json.addProperty("portfolio_transactions", portfolioTransactions); //$NON-NLS-1$
            json.addProperty("transactions", transactions); //$NON-NLS-1$
            json.addProperty("historical_prices", historicalPrices); //$NON-NLS-1$
            return json;
        }
    }

    private record PeriodSelection(String code, Interval interval, String filterUUID)
    {
    }

    private record DashboardSelection(PeriodSelection allTime, PeriodSelection currentYear)
    {
        static DashboardSelection read(Path input, String reportingCurrency) throws Exception
        {
            DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
            factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true); //$NON-NLS-1$
            factory.setFeature("http://xml.org/sax/features/external-general-entities", false); //$NON-NLS-1$
            factory.setFeature("http://xml.org/sax/features/external-parameter-entities", false); //$NON-NLS-1$
            factory.setFeature("http://apache.org/xml/features/nonvalidating/load-external-dtd", false); //$NON-NLS-1$
            factory.setXIncludeAware(false);
            factory.setExpandEntityReferences(false);
            factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, ""); //$NON-NLS-1$
            factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_SCHEMA, ""); //$NON-NLS-1$

            Document document;
            try (InputStream stream = Files.newInputStream(input))
            {
                document = factory.newDocumentBuilder().parse(stream);
            }

            Map<String, PeriodSelection> unique = new LinkedHashMap<>();
            NodeList dashboards = document.getElementsByTagName("dashboard"); //$NON-NLS-1$
            String currencyToken = reportingCurrency != null ? reportingCurrency.toUpperCase(Locale.ROOT) : ""; //$NON-NLS-1$

            for (int ii = 0; ii < dashboards.getLength(); ii++)
            {
                Element dashboard = (Element) dashboards.item(ii);
                String dashboardName = dashboard.getAttribute("name").toUpperCase(Locale.ROOT); //$NON-NLS-1$
                if (!currencyToken.isEmpty() && !dashboardName.contains(currencyToken))
                    continue;

                NodeList widgets = dashboard.getElementsByTagName("widget"); //$NON-NLS-1$
                for (int jj = 0; jj < widgets.getLength(); jj++)
                {
                    Element widget = (Element) widgets.item(jj);
                    if (!"TTWROR".equals(widget.getAttribute("type"))) //$NON-NLS-1$ //$NON-NLS-2$
                        continue;

                    Map<String, String> configuration = configuration(widget);
                    String code = configuration.get("REPORTING_PERIOD"); //$NON-NLS-1$
                    String dataSeries = configuration.get("DATA_SERIES"); //$NON-NLS-1$
                    if (code == null || dataSeries == null || !dataSeries.startsWith(CLIENT_FILTER_PREFIX))
                        continue;

                    Interval interval = ReportingPeriod.from(code).toInterval(LocalDate.now());
                    String filterUUID = dataSeries.substring(CLIENT_FILTER_PREFIX.length());
                    unique.putIfAbsent(code + '\u0000' + filterUUID,
                                    new PeriodSelection(code, interval, filterUUID));
                }
            }

            if (unique.isEmpty())
                throw new IOException("No TTWROR dashboard periods were found for currency " + reportingCurrency); //$NON-NLS-1$

            List<PeriodSelection> periods = new ArrayList<>(unique.values());
            PeriodSelection allTime = periods.stream().min(Comparator.comparing(period -> period.interval().getStart()))
                            .orElseThrow(() -> new IOException("All-time period was not found")); //$NON-NLS-1$

            Optional<PeriodSelection> currentYear = periods.stream().filter(period -> {
                LocalDate start = period.interval().getStart();
                LocalDate end = period.interval().getEnd();
                return start.plusDays(1).equals(LocalDate.of(end.getYear(), 1, 1))
                                && end.equals(LocalDate.of(end.getYear(), 12, 31));
            }).findFirst();

            return new DashboardSelection(allTime,
                            currentYear.orElseThrow(() -> new IOException("Current-year fixed period was not found"))); //$NON-NLS-1$
        }

        private static Map<String, String> configuration(Element widget)
        {
            Map<String, String> answer = new HashMap<>();
            NodeList configurations = widget.getChildNodes();
            for (int ii = 0; ii < configurations.getLength(); ii++)
            {
                Node node = configurations.item(ii);
                if (!(node instanceof Element element) || !"configuration".equals(element.getTagName())) //$NON-NLS-1$
                    continue;

                NodeList entries = element.getElementsByTagName("entry"); //$NON-NLS-1$
                for (int jj = 0; jj < entries.getLength(); jj++)
                {
                    NodeList strings = ((Element) entries.item(jj)).getElementsByTagName("string"); //$NON-NLS-1$
                    if (strings.getLength() >= 2)
                        answer.put(strings.item(0).getTextContent(), strings.item(1).getTextContent());
                }
            }
            return answer;
        }
    }

    private record FilterComponent(String type, String uuid, String name, String currency, int weight)
    {
    }

    private record ResolvedFilter(String type, String uuid, String name, List<FilterComponent> components,
                    List<String> accountClosureUUIDs, List<String> portfolioClosureUUIDs, Client filteredClient)
    {
    }

    private record AccountDiagnostic(String uuid, String name, String sourceCurrency, Double sourceBalance,
                    String reportingCurrency, Double convertedBalance)
    {
    }

    private record PortfolioDiagnostic(String uuid, String name, Double convertedMarketValue,
                    List<SecurityDiagnostic> securities)
    {
    }

    private record SecurityDiagnostic(String uuid, String name, String currency, Double shares, LocalDate priceDate,
                    Double appliedPrice, Double unconvertedMarketValue, Double convertedMarketValue)
    {
    }

    private record AppliedPriceDiagnostic(String securityUUID, String securityName, LocalDate requestedDate,
                    LocalDate rawPriceDate, Double rawPrice, LocalDate appliedPriceDate, Double appliedPrice,
                    String selectionType)
    {
    }

    private record PriceEvent(String securityUUID, String securityName, LocalDate requestedDate,
                    LocalDate rawPriceDate, Double rawPrice, LocalDate appliedPriceDate, Double appliedPrice,
                    String selectionType)
    {
    }

    private record TransactionDiagnostic(String ownerType, String ownerUUID, String ownerName, String uuid,
                    String dateTime, String type, String currency, Double amount, Double shares, String securityUUID,
                    String securityName)
    {
    }

    private static final class FxDiagnostics
    {
        private final Map<String, FxPairDiagnostic> pairs = new TreeMap<>();

        void registerPair(String sourceCurrency, String termCurrency)
        {
            if (sourceCurrency == null || termCurrency == null)
                return;
            FxPairDiagnostic pair = pair(sourceCurrency, termCurrency);
            if (sourceCurrency.equals(termCurrency))
            {
                pair.providers.add("IDENTITY"); //$NON-NLS-1$
                pair.conversionRequired = false;
            }
        }

        FxPairDiagnostic pair(String sourceCurrency, String termCurrency)
        {
            return pairs.computeIfAbsent(sourceCurrency + "/" + termCurrency, //$NON-NLS-1$
                            key -> new FxPairDiagnostic(sourceCurrency, termCurrency));
        }

        boolean hasFatalFallback()
        {
            return pairs.values().stream().anyMatch(pair -> pair.fallbackRateOneCount > 0);
        }

        JsonObject toJson()
        {
            JsonObject json = new JsonObject();
            json.add("currency_pairs", GSON.toJsonTree(pairs.values())); //$NON-NLS-1$
            int fallback = pairs.values().stream().mapToInt(pair -> pair.fallbackRateOneCount).sum();
            json.addProperty("fallback_rate_1_count", fallback); //$NON-NLS-1$
            json.add("empty_fx_series", GSON.toJsonTree(pairs.values().stream().filter(pair -> pair.emptySeries) //$NON-NLS-1$
                            .map(pair -> pair.sourceCurrency + "/" + pair.termCurrency).toList())); //$NON-NLS-1$
            json.addProperty("fatal_missing_fx", fallback > 0); //$NON-NLS-1$
            return json;
        }
    }

    private static final class FxPairDiagnostic
    {
        private final String sourceCurrency;
        private final String termCurrency;
        private boolean conversionRequired = true;
        private final Set<String> providers = new TreeSet<>();
        private final Set<String> composition = new TreeSet<>();
        private final Set<LocalDate> requestedRateDates = new TreeSet<>();
        private final Set<LocalDate> usedRateDates = new TreeSet<>();
        private int lookupCount;
        private int fallbackRateOneCount;
        private boolean emptySeries;

        private FxPairDiagnostic(String sourceCurrency, String termCurrency)
        {
            this.sourceCurrency = sourceCurrency;
            this.termCurrency = termCurrency;
        }
    }

    private static final class DiagnosticCurrencyConverter implements CurrencyConverter
    {
        private final CurrencyConverter delegate;
        private final ExchangeRateProviderFactory factory;
        private final FxDiagnostics diagnostics;

        private DiagnosticCurrencyConverter(CurrencyConverter delegate, ExchangeRateProviderFactory factory,
                        FxDiagnostics diagnostics)
        {
            this.delegate = delegate;
            this.factory = factory;
            this.diagnostics = diagnostics;
        }

        @Override
        public String getTermCurrency()
        {
            return delegate.getTermCurrency();
        }

        @Override
        public ExchangeRate getRate(LocalDate date, String currencyCode)
        {
            FxPairDiagnostic diagnostic = diagnostics.pair(currencyCode, getTermCurrency());
            diagnostic.requestedRateDates.add(date);
            diagnostic.lookupCount++;

            if (currencyCode.equals(getTermCurrency()))
            {
                diagnostic.conversionRequired = false;
                diagnostic.providers.add("IDENTITY"); //$NON-NLS-1$
                diagnostic.usedRateDates.add(date);
                return delegate.getRate(date, currencyCode);
            }

            ExchangeRateTimeSeries series = factory.getTimeSeries(currencyCode, getTermCurrency());
            registerSeries(diagnostic, series);
            Optional<ExchangeRate> lookup = series.lookupRate(date);
            boolean empty = series instanceof EmptyExchangeRateTimeSeries || lookup.isEmpty();
            if (empty)
            {
                diagnostic.emptySeries = true;
                diagnostic.fallbackRateOneCount++;
                throw new MissingExchangeRateException(
                                "Missing FX series/rate for " + currencyCode + "/" + getTermCurrency() + " at " + date); //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$
            }

            ExchangeRate rate = delegate.getRate(date, currencyCode);
            diagnostic.usedRateDates.add(rate.getTime());
            return rate;
        }

        private static void registerSeries(FxPairDiagnostic diagnostic, ExchangeRateTimeSeries series)
        {
            series.getProvider().map(ExchangeRateProvider::getName).ifPresent(diagnostic.providers::add);
            if (series.getProvider().isEmpty() && series.getComposition().isEmpty())
                diagnostic.providers.add(series.getClass().getSimpleName());
            for (ExchangeRateTimeSeries component : series.getComposition())
            {
                diagnostic.composition.add(component.getLabel());
                component.getProvider().map(ExchangeRateProvider::getName).ifPresent(diagnostic.providers::add);
            }
        }

        @Override
        public CurrencyConverter with(String currencyCode)
        {
            if (currencyCode.equals(getTermCurrency()))
                return this;
            return new DiagnosticCurrencyConverter(delegate.with(currencyCode), factory, diagnostics);
        }
    }

    private static final class MissingExchangeRateException extends RuntimeException
    {
        private static final long serialVersionUID = 1L;

        private MissingExchangeRateException(String message)
        {
            super(message);
        }
    }
}
