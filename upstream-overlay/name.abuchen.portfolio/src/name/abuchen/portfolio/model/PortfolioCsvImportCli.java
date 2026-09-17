package name.abuchen.portfolio.model;

import java.io.IOException;
import java.io.PushbackReader;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.time.Instant;
import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.time.format.ResolverStyle;
import java.util.ArrayList;
import java.util.EnumSet;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.UUID;

import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVParser;
import org.apache.commons.csv.CSVRecord;

import name.abuchen.portfolio.model.Transaction.Unit;
import name.abuchen.portfolio.money.CurrencyUnit;
import name.abuchen.portfolio.money.Money;
import name.abuchen.portfolio.money.Values;
import name.abuchen.portfolio.util.Isin;

/**
 * Headless adapter for the fixed CSV contract. The adapter mutates the
 * official Portfolio Performance model and persists it with ClientFactory;
 * it never writes an approximate XML representation itself.
 */
public final class PortfolioCsvImportCli
{
    private static final List<String> HEADERS = List.of("Date", "Type", "Transaction Currency", "ISIN", //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$ //$NON-NLS-4$
                    "Security Name", "Shares", "Fees", "Taxes", "Value", "Cash Account", //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$ //$NON-NLS-4$ //$NON-NLS-5$ //$NON-NLS-6$
                    "Securities Account", "Note"); //$NON-NLS-1$ //$NON-NLS-2$
    private static final List<String> INTERNAL_HEADERS = List.of("Date", "Type", "Transaction Currency", "ISIN", //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$ //$NON-NLS-4$
                    "Security Name", "Shares", "Fees", "Taxes", "Value", "Cash Account", //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$ //$NON-NLS-4$ //$NON-NLS-5$ //$NON-NLS-6$
                    "Securities Account", "Note", "_IdentityKey", "_SourceDocumentId", "_CashAccountUUID", //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$ //$NON-NLS-4$ //$NON-NLS-5$
                    "_SecuritiesAccountUUID", "_SecurityUUID"); //$NON-NLS-1$ //$NON-NLS-2$
    private static final DateTimeFormatter DATE_FORMAT = DateTimeFormatter.ofPattern("uuuu-MM-dd") //$NON-NLS-1$
                    .withResolverStyle(ResolverStyle.STRICT);
    private static final Instant FIXED_UPDATED_AT = Instant.EPOCH;

    private record Row(LocalDate date, String type, String currency, String isin, String securityName, long shares,
                    long fees, long taxes, long value, String cashAccount, String securitiesAccount, String note,
                    String identity, String sourceDocumentId, String cashAccountUuid, String securitiesAccountUuid,
                    String securityUuid, boolean internal, String source)
    {
    }

    private record Summary(int logicalTransactions, int accounts, int portfolios, int securities,
                    int accountTransactions, int portfolioTransactions)
    {
        String format()
        {
            return String.format(Locale.ROOT,
                            "OK|logical=%d|accounts=%d|portfolios=%d|securities=%d|accountTransactions=%d|portfolioTransactions=%d", //$NON-NLS-1$
                            logicalTransactions, accounts, portfolios, securities, accountTransactions,
                            portfolioTransactions);
        }
    }

    private record ApplySummary(int added, int skipped, Summary summary)
    {
        String format()
        {
            return String.format(Locale.ROOT,
                            "OK|added=%d|skipped=%d|logical=%d|accounts=%d|portfolios=%d|securities=%d|accountTransactions=%d|portfolioTransactions=%d", //$NON-NLS-1$
                            added, skipped, summary.logicalTransactions(), summary.accounts(), summary.portfolios(),
                            summary.securities(), summary.accountTransactions(), summary.portfolioTransactions());
        }
    }

    private PortfolioCsvImportCli()
    {
    }

    public static void main(String[] args)
    {
        try
        {
            if (args.length == 2 && "--verify".equals(args[0])) //$NON-NLS-1$
            {
                System.out.println(loadAndSummarize(Path.of(args[1])).format());
                return;
            }
            if (args.length == 3 && "--verify-import".equals(args[0])) //$NON-NLS-1$
            {
                Path csv = Path.of(args[1]).toAbsolutePath().normalize();
                Path xml = Path.of(args[2]).toAbsolutePath().normalize();
                List<Row> rows = readCsv(csv);
                Client client = loadClient(xml);
                auditClient(client);
                verifyRows(client, rows);
                System.out.println("OK|semanticRows=" + rows.size() + "|crossEntryMismatches=0"); //$NON-NLS-1$ //$NON-NLS-2$
                return;
            }
            if ((args.length == 5 || args.length == 6) && "--apply".equals(args[0])) //$NON-NLS-1$
            {
                Path csv = Path.of(args[1]).toAbsolutePath().normalize();
                Path existing = "-".equals(args[2]) ? null : Path.of(args[2]).toAbsolutePath().normalize(); //$NON-NLS-1$
                Path output = Path.of(args[3]).toAbsolutePath().normalize();
                String baseCurrency = args.length == 6 ? args[5].trim().toUpperCase(Locale.ROOT) : ""; //$NON-NLS-1$
                if (!"--base-currency".equals(args[4])) //$NON-NLS-1$
                    throw new IllegalArgumentException("Expected --base-currency before the optional value"); //$NON-NLS-1$
                System.out.println(apply(csv, existing, output, baseCurrency).format());
                return;
            }
            throw new IllegalArgumentException(
                            "Usage: PortfolioCsvImportCli --apply <csv> <existing.xml|-> <output.xml> --base-currency [CODE], --verify <xml>, or --verify-import <csv> <xml>"); //$NON-NLS-1$
        }
        catch (Exception e)
        {
            System.err.println("CSV_TO_XML_ERROR: " + e.getMessage()); //$NON-NLS-1$
            System.exit(65);
        }
    }

    private static ApplySummary apply(Path csv, Path existing, Path output, String configuredBaseCurrency)
                    throws IOException
    {
        if (!Files.isRegularFile(csv, LinkOption.NOFOLLOW_LINKS))
            throw new IOException("CSV file not found: " + csv); //$NON-NLS-1$
        List<Row> rows = readCsv(csv);
        if (rows.isEmpty())
            throw new IllegalArgumentException("CSV contains no transactions"); //$NON-NLS-1$

        Client client = existing == null ? new Client() : loadClient(existing);
        auditClient(client);
        initialiseBaseCurrency(client, rows, configuredBaseCurrency, existing == null);

        Set<String> ambiguousAccountNames = new HashSet<>();
        Set<String> ambiguousPortfolioNames = new HashSet<>();
        Set<String> ambiguousSecurityNames = new HashSet<>();
        Map<String, Account> accounts = indexAccounts(client, ambiguousAccountNames);
        Map<String, Portfolio> portfolios = indexPortfolios(client, ambiguousPortfolioNames);
        Map<String, Security> securitiesByName = new HashMap<>();
        Map<String, Security> securitiesByIsin = new HashMap<>();
        indexSecurities(client, securitiesByName, securitiesByIsin, ambiguousSecurityNames);
        Map<String, Set<String>> isinsByName = knownIsinsByName(rows, client);

        Set<String> transactionUuids = new HashSet<>();
        for (Account account : client.getAccounts())
            for (AccountTransaction transaction : account.getTransactions())
                transactionUuids.add(transaction.getUUID());

        List<Row> importRows = new ArrayList<>();
        Map<String, Row> legacyUniqueRows = new TreeMap<>();
        for (Row row : rows)
        {
            if (row.internal())
                importRows.add(row);
            else
                legacyUniqueRows.putIfAbsent(operationIdentity(row, resolvedSecurityKey(row, isinsByName)), row);
        }
        importRows.addAll(legacyUniqueRows.values());

        int before = transactionUuids.size();
        int skipped = 0;
        List<Row> addedRows = new ArrayList<>();
        for (Row row : importRows)
        {
            String securityKey = resolvedSecurityKey(row, isinsByName);
            String identity = row.internal() ? row.identity() : operationIdentity(row, securityKey);
            String accountUuid = accountTransactionUuid(row.type(), identity);
            if (transactionUuids.contains(accountUuid))
            {
                verifyRows(client, List.of(row));
                skipped++;
                continue;
            }

            Account account = getAccount(client, accounts, ambiguousAccountNames, row.cashAccount(), row.currency(),
                            row.cashAccountUuid(), existing == null);
            switch (row.type())
            {
                case "Deposit": //$NON-NLS-1$
                    addCashTransaction(account, row, identity, AccountTransaction.Type.DEPOSIT);
                    break;
                case "Removal": //$NON-NLS-1$
                    addCashTransaction(account, row, identity, AccountTransaction.Type.REMOVAL);
                    break;
                case "Buy": //$NON-NLS-1$
                case "Sell": //$NON-NLS-1$
                {
                    Portfolio portfolio = getPortfolio(client, portfolios, ambiguousPortfolioNames,
                                    row.securitiesAccount(), account, row.securitiesAccountUuid(),
                                    existing == null);
                    Security security = getSecurity(client, securitiesByName, securitiesByIsin,
                                    ambiguousSecurityNames, row, existing == null);
                    addBuySell(portfolio, account, security, row, identity);
                    break;
                }
                case "Dividend": //$NON-NLS-1$
                {
                    getPortfolio(client, portfolios, ambiguousPortfolioNames, row.securitiesAccount(), account,
                                    row.securitiesAccountUuid(), existing == null);
                    Security security = getSecurity(client, securitiesByName, securitiesByIsin,
                                    ambiguousSecurityNames, row, existing == null);
                    addDividend(account, security, row, identity);
                    break;
                }
                case "Taxes": //$NON-NLS-1$
                    addCashTransaction(account, row, identity, AccountTransaction.Type.TAXES);
                    break;
                case "Interest": //$NON-NLS-1$
                    addCashTransaction(account, row, identity, AccountTransaction.Type.INTEREST);
                    break;
                case "Interest Charge": //$NON-NLS-1$
                    addCashTransaction(account, row, identity, AccountTransaction.Type.INTEREST_CHARGE);
                    break;
                default:
                    throw new IllegalArgumentException("Unsupported transaction type: " + row.type()); //$NON-NLS-1$
            }
            transactionUuids.add(accountUuid);
            addedRows.add(row);
        }

        int added = transactionUuids.size() - before;
        Path parent = output.getParent();
        if (parent == null)
            throw new IOException("Output XML must have a parent directory"); //$NON-NLS-1$
        Files.createDirectories(parent);
        ClientFactory.saveAs(client, output.toFile(), null, EnumSet.of(SaveFlag.XML));
        Client reloaded = loadClient(output);
        auditClient(reloaded);
        verifyRows(reloaded, addedRows);
        Summary loaded = summarize(reloaded);
        if (loaded.logicalTransactions() != before + added)
            throw new IOException(String.format(Locale.ROOT,
                            "Official loader returned %d transactions; expected %d", loaded.logicalTransactions(), //$NON-NLS-1$
                            before + added));
        return new ApplySummary(added, skipped + rows.size() - importRows.size(), loaded);
    }

    private static Client loadClient(Path xml) throws IOException
    {
        if (!Files.isRegularFile(xml, LinkOption.NOFOLLOW_LINKS))
            throw new IOException("Portfolio XML is not a regular file: " + xml); //$NON-NLS-1$
        try (var input = Files.newInputStream(xml))
        {
            return ClientFactory.load(input);
        }
    }

    private static void initialiseBaseCurrency(Client client, List<Row> rows, String configured, boolean fresh)
    {
        if (!configured.isEmpty())
            validateCurrency(configured, "Configured base currency"); //$NON-NLS-1$
        if (!fresh)
        {
            String existing = client.getBaseCurrency();
            if (existing == null || existing.isBlank())
                throw new IllegalArgumentException("Existing Portfolio XML has no base currency"); //$NON-NLS-1$
            if (!configured.isEmpty() && !configured.equals(existing))
                throw new IllegalArgumentException("Configured base currency conflicts with existing Portfolio XML"); //$NON-NLS-1$
            return;
        }
        if (!configured.isEmpty())
        {
            client.setBaseCurrency(configured);
            return;
        }
        Set<String> currencies = new HashSet<>();
        for (Row row : rows)
            currencies.add(row.currency());
        if (currencies.size() != 1)
            throw new IllegalArgumentException(
                            "First CSV contains multiple currencies; configure portfolio.base_currency explicitly"); //$NON-NLS-1$
        client.setBaseCurrency(currencies.iterator().next());
    }

    private static Map<String, Account> indexAccounts(Client client, Set<String> ambiguousNames)
    {
        Map<String, Account> answer = new TreeMap<>();
        for (Account account : client.getAccounts())
        {
            String key = normalizedName(account.getName());
            if (ambiguousNames.contains(key))
                continue;
            if (answer.putIfAbsent(key, account) != null)
            {
                answer.remove(key);
                ambiguousNames.add(key);
            }
        }
        return answer;
    }

    private static Map<String, Portfolio> indexPortfolios(Client client, Set<String> ambiguousNames)
    {
        Map<String, Portfolio> answer = new TreeMap<>();
        for (Portfolio portfolio : client.getPortfolios())
        {
            String key = normalizedName(portfolio.getName());
            if (ambiguousNames.contains(key))
                continue;
            if (answer.putIfAbsent(key, portfolio) != null)
            {
                answer.remove(key);
                ambiguousNames.add(key);
            }
        }
        return answer;
    }

    private static void indexSecurities(Client client, Map<String, Security> byName, Map<String, Security> byIsin,
                    Set<String> ambiguousNames)
    {
        for (Security security : client.getSecurities())
        {
            String nameKey = securityNameKey(security.getName(), security.getCurrencyCode());
            if (!ambiguousNames.contains(nameKey) && byName.putIfAbsent(nameKey, security) != null)
            {
                byName.remove(nameKey);
                ambiguousNames.add(nameKey);
            }
            if (security.getIsin() != null && !security.getIsin().isBlank()
                            && byIsin.putIfAbsent(security.getIsin(), security) != null)
                throw new IllegalArgumentException("Existing Portfolio XML contains duplicate ISIN: " + security.getIsin()); //$NON-NLS-1$
        }
    }

    private static Map<String, Set<String>> knownIsinsByName(List<Row> rows, Client client)
    {
        Map<String, Set<String>> answer = new HashMap<>();
        for (Security security : client.getSecurities())
            if (security.getIsin() != null && !security.getIsin().isBlank())
                answer.computeIfAbsent(securityNameKey(security.getName(), security.getCurrencyCode()),
                                ignored -> new HashSet<>())
                                .add(security.getIsin());
        for (Row row : rows)
            if (!row.securityName().isEmpty() && !row.isin().isEmpty())
                answer.computeIfAbsent(securityNameKey(row.securityName(), row.currency()), ignored -> new HashSet<>())
                                .add(row.isin());
        return answer;
    }

    private static List<Row> readCsv(Path path) throws IOException
    {
        List<Row> answer = new ArrayList<>();
        try (PushbackReader reader = new PushbackReader(Files.newBufferedReader(path, StandardCharsets.UTF_8), 1))
        {
            int first = reader.read();
            if (first != 0xFEFF && first != -1)
                reader.unread(first);
            CSVFormat format = CSVFormat.DEFAULT.builder().setHeader().setSkipHeaderRecord(true).get();
            try (CSVParser parser = new CSVParser(reader, format))
            {
                List<String> headers = parser.getHeaderNames();
                boolean internal = headers.equals(INTERNAL_HEADERS);
                if (!internal && !headers.equals(HEADERS))
                    throw new IllegalArgumentException(path.getFileName() + ": headers must exactly match " //$NON-NLS-1$
                                    + String.join(",", HEADERS) + " or the watcher-internal contract"); //$NON-NLS-1$ //$NON-NLS-2$
                for (CSVRecord record : parser)
                {
                    boolean blank = true;
                    for (String value : record)
                        blank &= value.trim().isEmpty();
                    if (blank)
                        continue;
                    if (!record.isConsistent() || record.size() != headers.size())
                        throw new IllegalArgumentException(path.getFileName() + ": line " //$NON-NLS-1$
                                        + (record.getRecordNumber() + 1) + " has the wrong column count"); //$NON-NLS-1$
                    answer.add(parseRow(path, record, internal));
                }
            }
        }
        return answer;
    }

    private static Row parseRow(Path path, CSVRecord record, boolean internal)
    {
        String source = path.getFileName() + ":" + (record.getRecordNumber() + 1); //$NON-NLS-1$
        Map<String, String> values = new HashMap<>();
        for (String header : HEADERS)
        {
            String value = record.get(header).trim();
            if ("-".equals(value)) //$NON-NLS-1$
                throw new IllegalArgumentException(source + ": use an empty value instead of '-' in " + header); //$NON-NLS-1$ //$NON-NLS-2$
            values.put(header, value);
        }
        String identity = internal ? record.get("_IdentityKey").trim() : ""; //$NON-NLS-1$ //$NON-NLS-2$
        String sourceDocumentId = internal ? record.get("_SourceDocumentId").trim() : ""; //$NON-NLS-1$ //$NON-NLS-2$
        String cashAccountUuid = internal ? validOptionalUuid(record.get("_CashAccountUUID").trim(), source, //$NON-NLS-1$
                        "_CashAccountUUID") : ""; //$NON-NLS-1$ //$NON-NLS-2$
        String securitiesAccountUuid = internal ? validOptionalUuid(record.get("_SecuritiesAccountUUID").trim(), //$NON-NLS-1$
                        source, "_SecuritiesAccountUUID") : ""; //$NON-NLS-1$ //$NON-NLS-2$
        String securityUuid = internal ? validOptionalUuid(record.get("_SecurityUUID").trim(), source, //$NON-NLS-1$
                        "_SecurityUUID") : ""; //$NON-NLS-1$ //$NON-NLS-2$
        if (internal && !identity.matches("(?:v2:[0-9a-f]{64}|v1:[0-9a-f]{64}:[1-9][0-9]*)")) //$NON-NLS-1$
            throw new IllegalArgumentException(source + ": invalid watcher identity key"); //$NON-NLS-1$

        LocalDate date;
        try
        {
            date = LocalDate.parse(values.get("Date"), DATE_FORMAT); //$NON-NLS-1$
        }
        catch (DateTimeParseException e)
        {
            throw new IllegalArgumentException(source + ": Date must use YYYY-MM-DD"); //$NON-NLS-1$
        }
        String type = values.get("Type"); //$NON-NLS-1$
        if (!Set.of("Deposit", "Removal", "Buy", "Sell", "Dividend", "Taxes", "Interest", //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$ //$NON-NLS-4$ //$NON-NLS-5$ //$NON-NLS-6$ //$NON-NLS-7$
                        "Interest Charge").contains(type)) //$NON-NLS-1$
            throw new IllegalArgumentException(source + ": unsupported Type: " + type); //$NON-NLS-1$
        String currency = values.get("Transaction Currency").toUpperCase(Locale.ROOT); //$NON-NLS-1$
        validateCurrency(currency, source + ": Transaction Currency"); //$NON-NLS-1$

        String isin = values.get("ISIN").toUpperCase(Locale.ROOT); //$NON-NLS-1$
        if (!isin.isEmpty() && !Isin.isValid(isin))
            throw new IllegalArgumentException(source + ": invalid ISIN: " + isin); //$NON-NLS-1$
        String securityName = values.get("Security Name"); //$NON-NLS-1$
        long value = parseScaled(values.get("Value"), Values.Money.factor(), Values.Money.precision(), true,
                        source + ": Value"); //$NON-NLS-1$
        long fees = parseScaled(values.get("Fees"), Values.Money.factor(), Values.Money.precision(), false,
                        source + ": Fees"); //$NON-NLS-1$
        long taxes = parseScaled(values.get("Taxes"), Values.Money.factor(), Values.Money.precision(), false,
                        source + ": Taxes"); //$NON-NLS-1$
        String sharesText = values.get("Shares"); //$NON-NLS-1$
        long shares = sharesText.isEmpty() ? 0
                        : parseScaled(sharesText, Values.Share.factor(), Values.Share.precision(), true,
                                        source + ": Shares"); //$NON-NLS-1$
        String cashAccount = values.get("Cash Account"); //$NON-NLS-1$
        String securitiesAccount = values.get("Securities Account"); //$NON-NLS-1$
        if (cashAccount.isEmpty())
            cashAccount = "Cash " + currency; //$NON-NLS-1$

        boolean securityType = Set.of("Buy", "Sell", "Dividend").contains(type); //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$
        if (!securityType)
        {
            if (!isin.isEmpty() || !securityName.isEmpty())
                throw new IllegalArgumentException(source + ": " + type + " security fields must be empty"); //$NON-NLS-1$ //$NON-NLS-2$
            if (!sharesText.isEmpty())
                throw new IllegalArgumentException(source + ": " + type + " Shares must be empty"); //$NON-NLS-1$ //$NON-NLS-2$
            if (!securitiesAccount.isEmpty())
                throw new IllegalArgumentException(source + ": " + type + " Securities Account must be empty"); //$NON-NLS-1$ //$NON-NLS-2$
            if (fees != 0 || taxes != 0)
                throw new IllegalArgumentException(source + ": " + type + " Fees and Taxes must be empty or zero"); //$NON-NLS-1$ //$NON-NLS-2$
        }
        else
        {
            if (securityName.isEmpty())
                throw new IllegalArgumentException(source + ": Security Name is required for " + type); //$NON-NLS-1$
            if (securitiesAccount.isEmpty())
                securitiesAccount = "Portfolio " + currency; //$NON-NLS-1$
            if (Set.of("Buy", "Sell").contains(type) && shares == 0) //$NON-NLS-1$ //$NON-NLS-2$
                throw new IllegalArgumentException(source + ": " + type + " Shares must be greater than zero"); //$NON-NLS-1$ //$NON-NLS-2$
            if (type.equals("Buy") && value <= fees + taxes) //$NON-NLS-1$
                throw new IllegalArgumentException(source + ": Buy Value must exceed Fees plus Taxes"); //$NON-NLS-1$
        }
        return new Row(date, type, currency, isin, securityName, shares, fees, taxes, value, cashAccount,
                        securitiesAccount, values.get("Note"), identity, sourceDocumentId, cashAccountUuid, //$NON-NLS-1$
                        securitiesAccountUuid, securityUuid, internal, source);
    }

    private static String validOptionalUuid(String value, String source, String field)
    {
        if (value.isEmpty())
            return value;
        try
        {
            return UUID.fromString(value).toString();
        }
        catch (IllegalArgumentException e)
        {
            throw new IllegalArgumentException(source + ": " + field + " must be a UUID"); //$NON-NLS-1$ //$NON-NLS-2$
        }
    }

    private static long parseScaled(String value, int factor, int precision, boolean positive, String label)
    {
        if (value.isEmpty())
        {
            if (positive)
                throw new IllegalArgumentException(label + " is required"); //$NON-NLS-1$
            return 0;
        }
        try
        {
            BigDecimal parsed = new BigDecimal(value);
            if (parsed.scale() > precision)
                parsed = parsed.setScale(precision, RoundingMode.UNNECESSARY);
            if (positive ? parsed.signum() <= 0 : parsed.signum() < 0)
                throw new IllegalArgumentException(label + (positive ? " must be greater than zero" : " must not be negative")); //$NON-NLS-1$ //$NON-NLS-2$
            return parsed.multiply(BigDecimal.valueOf(factor)).longValueExact();
        }
        catch (ArithmeticException | NumberFormatException e)
        {
            throw new IllegalArgumentException(label + " has invalid precision or numeric format"); //$NON-NLS-1$
        }
    }

    private static void validateCurrency(String currency, String label)
    {
        if (!currency.matches("[A-Z]{3}") || CurrencyUnit.getInstance(currency) == null) //$NON-NLS-1$
            throw new IllegalArgumentException(label + " must be a supported three-letter ISO currency"); //$NON-NLS-1$
    }

    private static Account getAccount(Client client, Map<String, Account> accounts, Set<String> ambiguousNames,
                    String name, String currency, String configuredUuid, boolean allowCreate)
    {
        String key = normalizedName(name);
        Account byName = ambiguousNames.contains(key) ? null : accounts.get(key);
        Account byUuid = configuredUuid.isEmpty() ? null
                        : client.getAccounts().stream().filter(item -> configuredUuid.equals(item.getUUID())).findFirst()
                                        .orElse(null);
        if (!configuredUuid.isEmpty() && byUuid == null && !allowCreate)
            throw new IllegalArgumentException("Mapped Cash Account UUID does not exist in the master XML: " //$NON-NLS-1$
                            + configuredUuid);
        if (configuredUuid.isEmpty() && ambiguousNames.contains(key))
            throw new IllegalArgumentException("Cash Account exact name is ambiguous in the master XML: " + name); //$NON-NLS-1$
        if (byUuid != null && byName != null && byUuid != byName)
            throw new IllegalArgumentException("Mapped Cash Account UUID and exact name refer to different objects: " //$NON-NLS-1$
                            + name);
        Account existing = byUuid != null ? byUuid : byName;
        if (existing != null)
        {
            if (!name.isEmpty() && !normalizedName(existing.getName()).equals(normalizedName(name)))
                throw new IllegalArgumentException("Mapped Cash Account name conflicts with the master XML: " + name); //$NON-NLS-1$
            if (!configuredUuid.isEmpty() && !configuredUuid.equals(existing.getUUID()))
                throw new IllegalArgumentException("Mapped Cash Account UUID conflicts with the master XML: " + name); //$NON-NLS-1$
            if (!currency.equals(existing.getCurrencyCode()))
                throw new IllegalArgumentException("Cash Account is used with multiple currencies: " + name); //$NON-NLS-1$
            return existing;
        }
        if (!allowCreate)
            throw new IllegalArgumentException("Cash Account does not exist in the master XML: " + name); //$NON-NLS-1$
        if (name.isEmpty())
            throw new IllegalArgumentException("A new Cash Account requires an exact name"); //$NON-NLS-1$
        String uuid = configuredUuid.isEmpty() ? deterministicUuid("account|" + key + "|" + currency) //$NON-NLS-1$ //$NON-NLS-2$
                        : configuredUuid;
        Account account = new Account(uuid, name);
        account.setCurrencyCode(currency);
        account.setUpdatedAt(FIXED_UPDATED_AT);
        client.addAccount(account);
        accounts.put(key, account);
        return account;
    }

    private static Portfolio getPortfolio(Client client, Map<String, Portfolio> portfolios,
                    Set<String> ambiguousNames, String name, Account account, String configuredUuid,
                    boolean allowCreate)
    {
        String key = normalizedName(name);
        Portfolio byName = ambiguousNames.contains(key) ? null : portfolios.get(key);
        Portfolio byUuid = configuredUuid.isEmpty() ? null
                        : client.getPortfolios().stream().filter(item -> configuredUuid.equals(item.getUUID())).findFirst()
                                        .orElse(null);
        if (!configuredUuid.isEmpty() && byUuid == null && !allowCreate)
            throw new IllegalArgumentException("Mapped Securities Account UUID does not exist in the master XML: " //$NON-NLS-1$
                            + configuredUuid);
        if (configuredUuid.isEmpty() && ambiguousNames.contains(key))
            throw new IllegalArgumentException(
                            "Securities Account exact name is ambiguous in the master XML: " + name); //$NON-NLS-1$
        if (byUuid != null && byName != null && byUuid != byName)
            throw new IllegalArgumentException(
                            "Mapped Securities Account UUID and exact name refer to different objects: " + name); //$NON-NLS-1$
        Portfolio existing = byUuid != null ? byUuid : byName;
        if (existing != null)
        {
            if (!name.isEmpty() && !normalizedName(existing.getName()).equals(normalizedName(name)))
                throw new IllegalArgumentException(
                                "Mapped Securities Account name conflicts with the master XML: " + name); //$NON-NLS-1$
            if (!configuredUuid.isEmpty() && !configuredUuid.equals(existing.getUUID()))
                throw new IllegalArgumentException(
                                "Mapped Securities Account UUID conflicts with the master XML: " + name); //$NON-NLS-1$
            if (existing.getReferenceAccount() != account)
                throw new IllegalArgumentException("Securities Account is linked to another Cash Account: " + name); //$NON-NLS-1$
            return existing;
        }
        if (!allowCreate)
            throw new IllegalArgumentException("Securities Account does not exist in the master XML: " + name); //$NON-NLS-1$
        if (name.isEmpty())
            throw new IllegalArgumentException("A new Securities Account requires an exact name"); //$NON-NLS-1$
        String accountIdentity = normalizedName(account.getName()) + "|" + account.getCurrencyCode(); //$NON-NLS-1$
        String uuid = configuredUuid.isEmpty()
                        ? deterministicUuid("portfolio|" + key + "|" + accountIdentity) : configuredUuid; //$NON-NLS-1$ //$NON-NLS-2$
        Portfolio portfolio = new Portfolio(uuid, name);
        portfolio.setReferenceAccount(account);
        portfolio.setUpdatedAt(FIXED_UPDATED_AT);
        client.addPortfolio(portfolio);
        portfolios.put(key, portfolio);
        return portfolio;
    }

    private static Security getSecurity(Client client, Map<String, Security> byName, Map<String, Security> byIsin,
                    Set<String> ambiguousNames, Row row, boolean allowCreate)
    {
        String nameKey = securityNameKey(row.securityName(), row.currency());
        Security byNameMatch = ambiguousNames.contains(nameKey) ? null : byName.get(nameKey);
        Security byIsinMatch = row.isin().isEmpty() ? null : byIsin.get(row.isin());
        Security byUuid = row.securityUuid().isEmpty() ? null
                        : client.getSecurities().stream().filter(item -> row.securityUuid().equals(item.getUUID()))
                                        .findFirst().orElse(null);
        if (!row.securityUuid().isEmpty() && byUuid == null && !allowCreate)
            throw new IllegalArgumentException("Mapped Security UUID does not exist in the master XML: " //$NON-NLS-1$
                            + row.securityUuid());
        if (row.securityUuid().isEmpty() && row.isin().isEmpty() && ambiguousNames.contains(nameKey))
            throw new IllegalArgumentException("Security exact name is ambiguous in the master XML: " //$NON-NLS-1$
                            + row.securityName());
        if (byUuid != null && byIsinMatch != null && byIsinMatch != byUuid)
            throw new IllegalArgumentException("Mapped Security UUID and ISIN disagree"); //$NON-NLS-1$
        Security security = byUuid != null ? byUuid : (!row.isin().isEmpty() ? byIsinMatch : byNameMatch);
        if (security != null)
        {
            if (!row.securityName().isEmpty()
                            && !securityNameKey(security.getName(), security.getCurrencyCode()).equals(nameKey))
                throw new IllegalArgumentException("ISIN maps to a conflicting security name: " + row.isin()); //$NON-NLS-1$
            if (!row.securityUuid().isEmpty() && !row.securityUuid().equals(security.getUUID()))
                throw new IllegalArgumentException("Mapped Security UUID conflicts with the master XML"); //$NON-NLS-1$
            if (!row.currency().equals(security.getCurrencyCode()))
                throw new IllegalArgumentException("Security is used with multiple currencies: " + row.securityName()); //$NON-NLS-1$
            if (!row.isin().isEmpty())
            {
                if (security.getIsin() != null && !security.getIsin().isBlank()
                                && !row.isin().equals(security.getIsin()))
                    throw new IllegalArgumentException("Security name maps to a conflicting ISIN: " + row.securityName()); //$NON-NLS-1$
                if (security.getIsin() == null || security.getIsin().isBlank())
                {
                    if (!allowCreate)
                        throw new IllegalArgumentException(
                                        "Existing Security has no ISIN required by the import: " + row.securityName()); //$NON-NLS-1$
                    security.setIsin(row.isin());
                    byIsin.put(row.isin(), security);
                }
            }
            return security;
        }

        if (!allowCreate)
            throw new IllegalArgumentException("Security does not exist in the master XML: " + row.securityName()); //$NON-NLS-1$
        if (row.securityName().isEmpty())
            throw new IllegalArgumentException("A new Security requires an exact name"); //$NON-NLS-1$
        String key = row.isin().isEmpty() ? "name:" + nameKey : "isin:" + row.isin(); //$NON-NLS-1$ //$NON-NLS-2$
        String uuid = row.securityUuid().isEmpty() ? deterministicUuid("security|" + key) : row.securityUuid(); //$NON-NLS-1$
        security = new Security(uuid);
        security.setName(row.securityName());
        security.setIsin(row.isin().isEmpty() ? null : row.isin());
        security.setCurrencyCode(row.currency());
        security.setUpdatedAt(FIXED_UPDATED_AT);
        client.addSecurity(security);
        Security priorByName = byName.get(nameKey);
        if (priorByName != null && priorByName != security)
        {
            byName.remove(nameKey);
            ambiguousNames.add(nameKey);
        }
        else if (!ambiguousNames.contains(nameKey))
        {
            byName.put(nameKey, security);
        }
        if (!row.isin().isEmpty())
            byIsin.put(row.isin(), security);
        return security;
    }

    private static void addCashTransaction(Account account, Row row, String identity, AccountTransaction.Type type)
    {
        AccountTransaction transaction = new AccountTransaction(accountTransactionUuid(row.type(), identity));
        transaction.setType(type);
        setCommon(transaction, row);
        account.addTransaction(transaction);
    }

    private static void addBuySell(Portfolio portfolio, Account account, Security security, Row row, String identity)
    {
        boolean buy = "Buy".equals(row.type()); //$NON-NLS-1$
        String operation = buy ? "buy" : "sell"; //$NON-NLS-1$ //$NON-NLS-2$
        PortfolioTransaction portfolioTransaction = new PortfolioTransaction(
                        deterministicUuid(operation + "-portfolio|" + identity)); //$NON-NLS-1$
        AccountTransaction accountTransaction = new AccountTransaction(accountTransactionUuid(row.type(), identity));
        BuySellEntry entry = new BuySellEntry(portfolio, portfolioTransaction, account, accountTransaction);
        entry.setType(buy ? PortfolioTransaction.Type.BUY : PortfolioTransaction.Type.SELL);
        entry.setSecurity(security);
        entry.setDate(row.date().atStartOfDay());
        entry.setAmount(row.value());
        entry.setCurrencyCode(row.currency());
        entry.setShares(row.shares());
        entry.setNote(emptyToNull(row.note()));
        addUnits(portfolioTransaction, row);
        portfolioTransaction.setUpdatedAt(FIXED_UPDATED_AT);
        accountTransaction.setUpdatedAt(FIXED_UPDATED_AT);
        entry.insert();
    }

    private static void addDividend(Account account, Security security, Row row, String identity)
    {
        AccountTransaction transaction = new AccountTransaction(accountTransactionUuid("Dividend", identity)); //$NON-NLS-1$
        transaction.setType(AccountTransaction.Type.DIVIDENDS);
        transaction.setSecurity(security);
        transaction.setShares(row.shares());
        setCommon(transaction, row);
        addUnits(transaction, row);
        transaction.setUpdatedAt(FIXED_UPDATED_AT);
        account.addTransaction(transaction);
    }

    private static void setCommon(AccountTransaction transaction, Row row)
    {
        transaction.setDateTime(row.date().atStartOfDay());
        transaction.setCurrencyCode(row.currency());
        transaction.setAmount(row.value());
        transaction.setNote(emptyToNull(row.note()));
        transaction.setUpdatedAt(FIXED_UPDATED_AT);
    }

    private static void addUnits(Transaction transaction, Row row)
    {
        if (row.fees() != 0)
            transaction.addUnit(new Unit(Unit.Type.FEE, Money.of(row.currency(), row.fees())));
        if (row.taxes() != 0)
            transaction.addUnit(new Unit(Unit.Type.TAX, Money.of(row.currency(), row.taxes())));
    }

    private static String resolvedSecurityKey(Row row, Map<String, Set<String>> isinsByName)
    {
        if (!row.isin().isEmpty())
            return "isin:" + row.isin(); //$NON-NLS-1$
        Set<String> known = isinsByName.get(securityNameKey(row.securityName(), row.currency()));
        if (known != null && known.size() == 1)
            return "isin:" + known.iterator().next(); //$NON-NLS-1$
        return row.securityName().isEmpty() ? ""
                        : "name:" + securityNameKey(row.securityName(), row.currency()); //$NON-NLS-1$ //$NON-NLS-2$
    }

    private static String operationIdentity(Row row, String securityKey)
    {
        return String.join("\u001f", row.date().toString(), row.type(), row.currency(), securityKey, //$NON-NLS-1$
                        Long.toString(row.shares()), Long.toString(row.fees()), Long.toString(row.taxes()),
                        Long.toString(row.value()), normalizedName(row.cashAccount()),
                        normalizedName(row.securitiesAccount()), row.note());
    }

    private static String accountTransactionUuid(String type, String identity)
    {
        return switch (type)
        {
            case "Deposit" -> deterministicUuid("deposit|" + identity); //$NON-NLS-1$ //$NON-NLS-2$
            case "Removal" -> deterministicUuid("removal|" + identity); //$NON-NLS-1$ //$NON-NLS-2$
            case "Buy" -> deterministicUuid("buy-account|" + identity); //$NON-NLS-1$ //$NON-NLS-2$
            case "Sell" -> deterministicUuid("sell-account|" + identity); //$NON-NLS-1$ //$NON-NLS-2$
            case "Dividend" -> deterministicUuid("dividend|" + identity); //$NON-NLS-1$ //$NON-NLS-2$
            case "Taxes" -> deterministicUuid("taxes|" + identity); //$NON-NLS-1$ //$NON-NLS-2$
            case "Interest" -> deterministicUuid("interest|" + identity); //$NON-NLS-1$ //$NON-NLS-2$
            case "Interest Charge" -> deterministicUuid("interest-charge|" + identity); //$NON-NLS-1$ //$NON-NLS-2$
            default -> throw new IllegalArgumentException("Unsupported transaction type: " + type); //$NON-NLS-1$
        };
    }

    private static String normalizedName(String value)
    {
        return value.trim().replaceAll("\\s+", " ").toLowerCase(Locale.ROOT); //$NON-NLS-1$ //$NON-NLS-2$
    }

    private static String securityNameKey(String name, String currency)
    {
        return normalizedName(name) + "\u001f" + currency;
    }

    private static String emptyToNull(String value)
    {
        return value.isEmpty() ? null : value;
    }

    private static String deterministicUuid(String value)
    {
        return UUID.nameUUIDFromBytes(("portfolio-csv-import|" + value).getBytes(StandardCharsets.UTF_8)).toString(); //$NON-NLS-1$
    }

    private static void auditClient(Client client)
    {
        Set<String> transactionUuids = new HashSet<>();
        for (Account account : client.getAccounts())
        {
            for (AccountTransaction transaction : account.getTransactions())
            {
                require(transaction.getUUID() != null && transactionUuids.add(transaction.getUUID()),
                                "Duplicate or missing transaction UUID: " + transaction.getUUID()); //$NON-NLS-1$
                if (transaction.getType() == AccountTransaction.Type.BUY
                                || transaction.getType() == AccountTransaction.Type.SELL)
                    auditBuySellAccountSide(client, account, transaction);
            }
        }
        for (Portfolio portfolio : client.getPortfolios())
        {
            for (PortfolioTransaction transaction : portfolio.getTransactions())
            {
                require(transaction.getUUID() != null && transactionUuids.add(transaction.getUUID()),
                                "Duplicate or missing transaction UUID: " + transaction.getUUID()); //$NON-NLS-1$
                if (transaction.getType() == PortfolioTransaction.Type.BUY
                                || transaction.getType() == PortfolioTransaction.Type.SELL)
                {
                    require(transaction.getCrossEntry() != null,
                                    "Portfolio Buy/Sell cross-entry is missing: " + transaction.getUUID()); //$NON-NLS-1$
                    Transaction counterpart = transaction.getCrossEntry().getCrossTransaction(transaction);
                    require(counterpart instanceof AccountTransaction,
                                    "Portfolio Buy/Sell counterpart is invalid: " + transaction.getUUID()); //$NON-NLS-1$
                    AccountTransaction accountTransaction = (AccountTransaction) counterpart;
                    Account accountOwner = client.getAccounts().stream()
                                    .filter(item -> item.getTransactions().contains(accountTransaction)).findFirst()
                                    .orElse(null);
                    require(accountOwner != null,
                                    "Portfolio Buy/Sell account owner is missing: " + transaction.getUUID()); //$NON-NLS-1$
                    auditBuySellAccountSide(client, accountOwner, accountTransaction);
                    require(portfolio.getReferenceAccount() == accountOwner,
                                    "Portfolio Buy/Sell reference account is inconsistent: " + transaction.getUUID()); //$NON-NLS-1$
                }
            }
        }
    }

    private static void auditBuySellAccountSide(Client client, Account account, AccountTransaction accountTransaction)
    {
        require(accountTransaction.getCrossEntry() != null,
                        "Account Buy/Sell cross-entry is missing: " + accountTransaction.getUUID()); //$NON-NLS-1$
        Transaction counterpart = accountTransaction.getCrossEntry().getCrossTransaction(accountTransaction);
        require(counterpart instanceof PortfolioTransaction,
                        "Account Buy/Sell counterpart is invalid: " + accountTransaction.getUUID()); //$NON-NLS-1$
        PortfolioTransaction portfolioTransaction = (PortfolioTransaction) counterpart;
        require(portfolioTransaction.getCrossEntry() != null
                        && portfolioTransaction.getCrossEntry().getCrossTransaction(portfolioTransaction) == accountTransaction,
                        "Buy/Sell cross-entry is not reciprocal: " + accountTransaction.getUUID()); //$NON-NLS-1$
        require(accountTransaction.getType().name().equals(portfolioTransaction.getType().name())
                        && accountTransaction.getDateTime().equals(portfolioTransaction.getDateTime())
                        && accountTransaction.getCurrencyCode().equals(portfolioTransaction.getCurrencyCode())
                        && accountTransaction.getAmount() == portfolioTransaction.getAmount()
                        && accountTransaction.getSecurity() == portfolioTransaction.getSecurity()
                        && accountTransaction.getShares() == 0 && portfolioTransaction.getShares() > 0,
                        "Buy/Sell cross-entry payload is inconsistent: " + accountTransaction.getUUID()); //$NON-NLS-1$
        require(accountTransaction.getUnitSum(Unit.Type.FEE).getAmount() == 0
                        && accountTransaction.getUnitSum(Unit.Type.TAX).getAmount() == 0,
                        "Buy/Sell account side contains fee/tax units: " + accountTransaction.getUUID()); //$NON-NLS-1$
        Portfolio owner = client.getPortfolios().stream()
                        .filter(item -> item.getTransactions().contains(portfolioTransaction)).findFirst().orElse(null);
        require(owner != null && owner.getReferenceAccount() == account,
                        "Buy/Sell portfolio owner/reference account is inconsistent: " + accountTransaction.getUUID()); //$NON-NLS-1$
    }

    private static void verifyRows(Client client, List<Row> rows)
    {
        Map<String, Set<String>> isinsByName = knownIsinsByName(rows, client);
        for (Row row : rows)
        {
            String identity = row.internal() ? row.identity()
                            : operationIdentity(row, resolvedSecurityKey(row, isinsByName));
            String transactionUuid = accountTransactionUuid(row.type(), identity);
            Account owner = null;
            AccountTransaction accountTransaction = null;
            for (Account account : client.getAccounts())
            {
                for (AccountTransaction candidate : account.getTransactions())
                {
                    if (transactionUuid.equals(candidate.getUUID()))
                    {
                        require(accountTransaction == null, "Duplicate account transaction UUID: " + transactionUuid); //$NON-NLS-1$
                        owner = account;
                        accountTransaction = candidate;
                    }
                }
            }
            require(accountTransaction != null, "Missing imported account transaction: " + transactionUuid); //$NON-NLS-1$
            require(expectedAccountType(row.type()) == accountTransaction.getType(),
                            "Account transaction type mismatch for " + transactionUuid); //$NON-NLS-1$
            require(row.date().atStartOfDay().equals(accountTransaction.getDateTime()),
                            "Account transaction date mismatch for " + transactionUuid); //$NON-NLS-1$
            require(row.currency().equals(accountTransaction.getCurrencyCode()),
                            "Account transaction currency mismatch for " + transactionUuid); //$NON-NLS-1$
            require(row.value() == accountTransaction.getAmount(),
                            "Account transaction amount mismatch for " + transactionUuid); //$NON-NLS-1$
            require(emptyToNull(row.note()) == null ? accountTransaction.getNote() == null
                            : emptyToNull(row.note()).equals(accountTransaction.getNote()),
                            "Account transaction note mismatch for " + transactionUuid); //$NON-NLS-1$
            require(owner != null && row.currency().equals(owner.getCurrencyCode()),
                            "Cash Account owner/currency mismatch for " + transactionUuid); //$NON-NLS-1$
            if (!row.cashAccountUuid().isEmpty())
                require(row.cashAccountUuid().equals(owner.getUUID()),
                                "Cash Account UUID mismatch for " + transactionUuid); //$NON-NLS-1$
            else
                require(normalizedName(row.cashAccount()).equals(normalizedName(owner.getName())),
                                "Cash Account name mismatch for " + transactionUuid); //$NON-NLS-1$

            if (Set.of("Buy", "Sell", "Dividend").contains(row.type())) //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$
            {
                Security security = accountTransaction.getSecurity();
                require(security != null, "Imported security link is missing for " + transactionUuid); //$NON-NLS-1$
                if (!row.securityUuid().isEmpty())
                    require(row.securityUuid().equals(security.getUUID()),
                                    "Security UUID mismatch for " + transactionUuid); //$NON-NLS-1$
                else if (!row.isin().isEmpty())
                    require(row.isin().equals(security.getIsin()), "Security ISIN mismatch for " + transactionUuid); //$NON-NLS-1$
                else
                    require(securityNameKey(row.securityName(), row.currency())
                                    .equals(securityNameKey(security.getName(), security.getCurrencyCode())),
                                    "Security name/currency mismatch for " + transactionUuid); //$NON-NLS-1$

                if (Set.of("Buy", "Sell").contains(row.type())) //$NON-NLS-1$ //$NON-NLS-2$
                {
                    require(accountTransaction.getCrossEntry() != null,
                                    "Buy/Sell cross-entry is missing for " + transactionUuid); //$NON-NLS-1$
                    Transaction counterpart = accountTransaction.getCrossEntry().getCrossTransaction(accountTransaction);
                    require(counterpart instanceof PortfolioTransaction,
                                    "Buy/Sell counterpart is not a PortfolioTransaction for " + transactionUuid); //$NON-NLS-1$
                    PortfolioTransaction portfolioTransaction = (PortfolioTransaction) counterpart;
                    String operation = "Buy".equals(row.type()) ? "buy" : "sell"; //$NON-NLS-1$ //$NON-NLS-2$ //$NON-NLS-3$
                    require(deterministicUuid(operation + "-portfolio|" + identity) //$NON-NLS-1$
                                    .equals(portfolioTransaction.getUUID()),
                                    "Portfolio transaction UUID mismatch for " + transactionUuid); //$NON-NLS-1$
                    require(!transactionUuid.equals(portfolioTransaction.getUUID()),
                                    "Buy/Sell account and portfolio UUIDs must differ for " + transactionUuid); //$NON-NLS-1$
                    require(("Buy".equals(row.type()) ? PortfolioTransaction.Type.BUY //$NON-NLS-1$
                                    : PortfolioTransaction.Type.SELL) == portfolioTransaction.getType(),
                                    "Portfolio transaction type mismatch for " + transactionUuid); //$NON-NLS-1$
                    require(portfolioTransaction.getCrossEntry() != null
                                    && portfolioTransaction.getCrossEntry().getCrossTransaction(portfolioTransaction)
                                                    == accountTransaction,
                                    "Buy/Sell reverse cross-entry is broken for " + transactionUuid); //$NON-NLS-1$
                    require(row.date().atStartOfDay().equals(portfolioTransaction.getDateTime())
                                    && row.currency().equals(portfolioTransaction.getCurrencyCode())
                                    && row.value() == portfolioTransaction.getAmount()
                                    && row.shares() == portfolioTransaction.getShares()
                                    && portfolioTransaction.getSecurity() == security,
                                    "Buy/Sell counterpart payload mismatch for " + transactionUuid); //$NON-NLS-1$
                    require(accountTransaction.getShares() == 0,
                                    "Buy/Sell account-side shares must be zero for " + transactionUuid); //$NON-NLS-1$
                    require(accountTransaction.getUnitSum(Unit.Type.FEE).getAmount() == 0
                                    && accountTransaction.getUnitSum(Unit.Type.TAX).getAmount() == 0,
                                    "Buy/Sell units must exist only on the portfolio side for " + transactionUuid); //$NON-NLS-1$
                    require(emptyToNull(row.note()) == null ? portfolioTransaction.getNote() == null
                                    : emptyToNull(row.note()).equals(portfolioTransaction.getNote()),
                                    "Portfolio transaction note mismatch for " + transactionUuid); //$NON-NLS-1$
                    Portfolio portfolioOwner = client.getPortfolios().stream()
                                    .filter(item -> item.getTransactions().contains(portfolioTransaction)).findFirst()
                                    .orElse(null);
                    require(portfolioOwner != null, "Portfolio owner is missing for " + transactionUuid); //$NON-NLS-1$
                    require(portfolioOwner.getReferenceAccount() == owner,
                                    "Portfolio reference Cash Account mismatch for " + transactionUuid); //$NON-NLS-1$
                    if (!row.securitiesAccountUuid().isEmpty())
                        require(row.securitiesAccountUuid().equals(portfolioOwner.getUUID()),
                                        "Securities Account UUID mismatch for " + transactionUuid); //$NON-NLS-1$
                    else
                        require(normalizedName(row.securitiesAccount()).equals(normalizedName(portfolioOwner.getName())),
                                        "Securities Account name mismatch for " + transactionUuid); //$NON-NLS-1$
                    verifyUnits(portfolioTransaction, row, transactionUuid);
                }
                else
                {
                    require(accountTransaction.getCrossEntry() == null,
                                    "Dividend must not have a cross-entry for " + transactionUuid); //$NON-NLS-1$
                    require(row.shares() == accountTransaction.getShares(),
                                    "Dividend shares mismatch for " + transactionUuid); //$NON-NLS-1$
                    verifyUnits(accountTransaction, row, transactionUuid);
                }
            }
            else
            {
                require(accountTransaction.getCrossEntry() == null,
                                "Cash-only transaction must not have a cross-entry for " + transactionUuid); //$NON-NLS-1$
                require(accountTransaction.getSecurity() == null,
                                "Cash-only transaction unexpectedly links a security: " + transactionUuid); //$NON-NLS-1$
                verifyUnits(accountTransaction, row, transactionUuid);
            }
        }
    }

    private static AccountTransaction.Type expectedAccountType(String type)
    {
        return switch (type)
        {
            case "Deposit" -> AccountTransaction.Type.DEPOSIT; //$NON-NLS-1$
            case "Removal" -> AccountTransaction.Type.REMOVAL; //$NON-NLS-1$
            case "Buy" -> AccountTransaction.Type.BUY; //$NON-NLS-1$
            case "Sell" -> AccountTransaction.Type.SELL; //$NON-NLS-1$
            case "Dividend" -> AccountTransaction.Type.DIVIDENDS; //$NON-NLS-1$
            case "Taxes" -> AccountTransaction.Type.TAXES; //$NON-NLS-1$
            case "Interest" -> AccountTransaction.Type.INTEREST; //$NON-NLS-1$
            case "Interest Charge" -> AccountTransaction.Type.INTEREST_CHARGE; //$NON-NLS-1$
            default -> throw new IllegalArgumentException("Unsupported transaction type: " + type); //$NON-NLS-1$
        };
    }

    private static void verifyUnits(Transaction transaction, Row row, String transactionUuid)
    {
        require(transaction.getUnitSum(Unit.Type.FEE).getAmount() == row.fees(),
                        "Fee-unit sum mismatch for " + transactionUuid); //$NON-NLS-1$
        require(transaction.getUnitSum(Unit.Type.TAX).getAmount() == row.taxes(),
                        "Tax-unit sum mismatch for " + transactionUuid); //$NON-NLS-1$
    }

    private static void require(boolean condition, String message)
    {
        if (!condition)
            throw new IllegalArgumentException(message);
    }

    private static Summary loadAndSummarize(Path xml) throws IOException
    {
        Client client = loadClient(xml);
        auditClient(client);
        return summarize(client);
    }

    private static Summary summarize(Client loaded)
    {
        int accountTransactions = loaded.getAccounts().stream().mapToInt(account -> account.getTransactions().size())
                        .sum();
        int portfolioTransactions = loaded.getPortfolios().stream()
                        .mapToInt(portfolio -> portfolio.getTransactions().size()).sum();
        return new Summary(accountTransactions, loaded.getAccounts().size(), loaded.getPortfolios().size(),
                        loaded.getSecurities().size(), accountTransactions, portfolioTransactions);
    }
}
