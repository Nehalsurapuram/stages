package com.autoimmune.orders;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;
import org.springframework.boot.actuate.health.Status;

class CatalogHealthIndicatorTest {

    @Test
    void upWhenCatalogEnabled() {
        assertEquals(Status.UP, new CatalogHealthIndicator(true).health().getStatus());
    }
}
