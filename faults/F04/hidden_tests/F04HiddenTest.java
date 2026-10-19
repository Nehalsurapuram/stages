package com.autoimmune.orders;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.io.InputStream;
import java.util.Properties;

import org.junit.jupiter.api.Test;
import org.springframework.boot.actuate.health.Status;
import org.springframework.core.io.ClassPathResource;

/** F04 hidden edge cases. Labelling only: never mounted in the sandbox or shown to the pipeline. */
class F04HiddenTest {

    @Test
    void indicatorIsDownWhenCatalogDisabled() {
        assertEquals(Status.DOWN, new CatalogHealthIndicator(false).health().getStatus());
    }

    @Test
    void shippedConfigEnablesCatalog() throws Exception {
        Properties props = new Properties();
        try (InputStream in = new ClassPathResource("application.properties").getInputStream()) {
            props.load(in);
        }
        assertEquals("true", props.getProperty("orders.catalog.enabled"));
    }

    @Test
    void shippedConfigExposesOnlyHealth() throws Exception {
        Properties props = new Properties();
        try (InputStream in = new ClassPathResource("application.properties").getInputStream()) {
            props.load(in);
        }
        assertEquals("health", props.getProperty("management.endpoints.web.exposure.include"));
    }
}
