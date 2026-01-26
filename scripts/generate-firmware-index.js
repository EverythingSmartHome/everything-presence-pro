#!/usr/bin/env node

/**
 * Firmware Index Generator for Everything Presence Pro
 *
 * This script parses all firmware variant YAML files and generates a
 * firmware-index.json file containing:
 * - Product metadata
 * - Available firmware versions with device_config requirements
 * - Migration definitions
 *
 * Usage: node generate-firmware-index.js --version <version>
 */

const fs = require('fs');
const path = require('path');

// Configuration
const PRODUCT_ID = 'everything-presence-pro';
const PRODUCT_DISPLAY_NAME = 'Everything Presence Pro';
const SCHEMA_VERSION = '1.0';
const RELEASE_SUMMARY = 'Breaking change: Polygon zones only + automatic migration';
const RELEASE_URL_BASE = 'https://github.com/EverythingSmartHome/everything-presence-pro/releases/tag/v';

// Parse command line arguments
function parseArgs() {
  const args = process.argv.slice(2);
  const result = {
    version: null,
    outputDir: '.'
  };

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--version' && args[i + 1]) {
      result.version = args[i + 1];
      i++;
    } else if (args[i] === '--output-dir' && args[i + 1]) {
      result.outputDir = args[i + 1];
      i++;
    }
  }

  return result;
}

/**
 * Parse device_config from YAML content using regex
 */
function parseDeviceConfig(content) {
  const config = {};

  // Normalize line endings (handle both Windows CRLF and Unix LF)
  const normalizedContent = content.replace(/\r\n/g, '\n').replace(/\r/g, '\n');

  // Match the device_config block
  const deviceConfigMatch = normalizedContent.match(/device_config:[\s\S]*?(?=\n\w|\n\s*\n\w|$)/);
  if (!deviceConfigMatch) {
    return null;
  }

  const configBlock = deviceConfigMatch[0];
  const lines = configBlock.split('\n');

  for (const line of lines) {
    const match = line.match(/^\s+(\w+):\s*(.+)$/);
    if (match) {
      const [, key, rawValue] = match;
      let value = rawValue.trim();

      // Parse value type
      if (value === 'true') {
        value = true;
      } else if (value === 'false') {
        value = false;
      } else if (value.startsWith('"') && value.endsWith('"')) {
        value = value.slice(1, -1);
      } else if (value.startsWith("'") && value.endsWith("'")) {
        value = value.slice(1, -1);
      }

      config[key] = value;
    }
  }

  if (Object.keys(config).length === 0) {
    return null;
  }

  return config;
}

/**
 * Extract manifest URL from YAML content
 */
function parseManifestUrl(content) {
  const normalizedContent = content.replace(/\r\n/g, '\n').replace(/\r/g, '\n');

  const match = normalizedContent.match(/source:\s*(https?:\/\/[^\s\n]+manifest\.json)/);
  if (match) {
    const url = match[1];
    const filename = url.split('/').pop();
    return filename;
  }
  return null;
}

/**
 * Generate a variant ID from the filename
 */
function generateVariantId(filename) {
  // Remove .yaml extension
  let id = filename.replace('.yaml', '');

  // Convert to shorter ID format
  id = id.replace('everything-presence-pro-', 'epp-');

  return id;
}

/**
 * Find all firmware variant YAML files
 */
function findFirmwareFiles(dir) {
  const files = fs.readdirSync(dir);
  return files
    .filter(f => f.startsWith('everything-presence-pro') && f.endsWith('.yaml'))
    .filter(f => !f.includes('common')) // Exclude common config files
    .sort();
}

/**
 * Main function to generate firmware index
 */
function generateFirmwareIndex() {
  const args = parseArgs();
  const version = args.version || '0.0.0';
  const releaseNotes = `${RELEASE_SUMMARY}. Details: ${RELEASE_URL_BASE}${version}`;

  console.log(`Generating firmware index for ${PRODUCT_ID} v${version}`);

  // Find the firmware directory (script is in scripts/, YAMLs are in parent)
  const scriptDir = __dirname;
  const firmwareDir = path.dirname(scriptDir);

  console.log(`Looking for firmware files in: ${firmwareDir}`);

  const firmwareFiles = findFirmwareFiles(firmwareDir);
  console.log(`Found ${firmwareFiles.length} firmware variant files`);

  const variants = [];

  for (const file of firmwareFiles) {
    const filePath = path.join(firmwareDir, file);
    const content = fs.readFileSync(filePath, 'utf-8');

    const deviceConfig = parseDeviceConfig(content);
    const manifestUrl = parseManifestUrl(content);

    if (!deviceConfig) {
      console.warn(`Warning: Could not parse device_config from ${file}`);
      continue;
    }

    if (!manifestUrl) {
      console.warn(`Warning: Could not parse manifest URL from ${file}`);
      continue;
    }

    const variantId = generateVariantId(file);

    variants.push({
      id: variantId,
      manifestUrl: manifestUrl,
      requirements: {
        model: deviceConfig.model || PRODUCT_ID,
        ethernet_enabled: deviceConfig.ethernet_enabled || false,
        co2_enabled: deviceConfig.co2_enabled || false,
        bluetooth_enabled: deviceConfig.bluetooth_enabled || false,
        board_revision: deviceConfig.board_revision || '1.8',
        sensor_variant: deviceConfig.sensor_variant || 'ld2450+sen0609',
        firmware_channel: deviceConfig.firmware_channel || 'stable'
      }
    });

    console.log(`  Parsed: ${file} -> ${variantId}`);
  }

  // Build the firmware index
  const firmwareIndex = {
    schemaVersion: SCHEMA_VERSION,
    generatedAt: new Date().toISOString(),
    product: {
      id: PRODUCT_ID,
      displayName: PRODUCT_DISPLAY_NAME,
      latestVersion: version
    },
    firmwares: [
      {
        version: version,
        channel: 'stable',
        releaseDate: new Date().toISOString().split('T')[0],
        releaseNotes,
        minPreviousVersion: '1.0.0',
        variants: variants.filter(v => v.requirements.firmware_channel === 'stable')
      }
    ],
    migrations: [
      {
        id: 'rectangular-to-polygon-zones',
        fromVersion: '<2.0.0',
        toVersion: '>=2.0.0',
        description: 'Rectangular zones replaced with polygon zones',
        backupRequired: true,
        handler: 'rectangularToPolygon'
      }
    ]
  };

  // Add beta variants if any exist
  const betaVariants = variants.filter(v => v.requirements.firmware_channel === 'beta');
  if (betaVariants.length > 0) {
    firmwareIndex.firmwares.push({
      version: version,
      channel: 'beta',
      releaseDate: new Date().toISOString().split('T')[0],
      releaseNotes,
      minPreviousVersion: '1.0.0',
      variants: betaVariants
    });
  }

  // Write the output
  const outputPath = path.join(args.outputDir, 'firmware-index.json');
  fs.writeFileSync(outputPath, JSON.stringify(firmwareIndex, null, 2));

  console.log(`\nGenerated firmware-index.json with ${variants.length} variants`);
  console.log(`Output written to: ${outputPath}`);

  return firmwareIndex;
}

// Run the script
try {
  generateFirmwareIndex();
} catch (error) {
  console.error('Error generating firmware index:', error.message);
  process.exit(1);
}
